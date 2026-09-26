"""Silver layer: typed Parquet per table with local time zones; rows that break the contract go to
quarantine with the rule, the column and the offending value. Nothing is dropped silently:
bronze rows = silver rows + quarantine rows, per table, or the run fails.
"""

import shutil
from dataclasses import dataclass
from pathlib import Path

import duckdb
import yaml

from app.core.logging import get_logger
from pipeline.contracts import Column, Contract, Reference
from pipeline.manifest import BronzeFile

log = get_logger("pipeline.silver")

PARQUET = "FORMAT parquet, COMPRESSION zstd"
PARTITIONED_PARQUET = (
    f"{PARQUET}, PARTITION_BY (partition_date), WRITE_PARTITION_COLUMNS true, "
    "FILENAME_PATTERN 'data', OVERWRITE_OR_IGNORE true"
)


@dataclass(frozen=True)
class Normalization:
    """Versioned reference data from config/normalization.yaml.

    Attributes:
        version: Version of the mapping file.
        countries: Source country name to ISO alpha-2 code.
        zones: Time zone per home country code.
        currencies: Local currency per home country code.
        documents: Identity document types coherent with each home country code.
        product_types: Source product type to normalized name.
    """

    version: int
    countries: dict[str, str]
    zones: dict[str, str]
    currencies: dict[str, str]
    documents: dict[str, list[str]]
    product_types: dict[str, str]

    def section(self, name: str) -> dict[str, str]:
        """Returns a value mapping by its section name in the YAML file.

        Args:
            name: `countries` or `product_types`.

        Returns:
            Source value to normalized value.
        """
        return {"countries": self.countries, "product_types": self.product_types}[name]


@dataclass(frozen=True)
class TableResult:
    """Row counts of one table in one run.

    Attributes:
        table: Table name.
        files: Bronze files read.
        bronze_rows: Rows in those files.
        silver_rows: Rows written to silver.
        quarantine_rows: Rows written to quarantine.
    """

    table: str
    files: int
    bronze_rows: int
    silver_rows: int
    quarantine_rows: int


def load_normalization(path: Path) -> Normalization:
    """Loads the versioned normalization mapping.

    Args:
        path: Path of the YAML file.

    Returns:
        The mapping.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    home = data["home_countries"]
    return Normalization(
        version=int(data["version"]),
        countries=dict(data["countries"]),
        zones={code: v["zone"] for code, v in home.items()},
        currencies={code: v["currency"] for code, v in home.items()},
        documents={code: list(v["documents"]) for code, v in home.items()},
        product_types=dict(data["product_types"]),
    )


def sql_str(value: str) -> str:
    """Quotes a string as a SQL literal.

    Args:
        value: Raw string.

    Returns:
        The string between single quotes, with inner quotes doubled.
    """
    return "'" + value.replace("'", "''") + "'"


def q(name: str) -> str:
    """Quotes an identifier.

    Args:
        name: Column or table name.

    Returns:
        The identifier between double quotes.
    """
    return '"' + name.replace('"', '""') + '"'


def silver_dir(data_dir: Path, table: str) -> Path:
    """Returns the silver folder of a table.

    Args:
        data_dir: Root data directory.
        table: Table name.

    Returns:
        `DATA_DIR/silver/<table>`.
    """
    return data_dir / "silver" / table


def quarantine_dir(data_dir: Path, table: str) -> Path:
    """Returns the quarantine folder of a table.

    Args:
        data_dir: Root data directory.
        table: Table name.

    Returns:
        `DATA_DIR/quarantine/<table>`.
    """
    return data_dir / "quarantine" / table


def parse_expr(column: Column, alias: str = "r") -> str:
    """Returns the SQL that parses a raw text value into the column's type, or NULL if it cannot.

    Parsing is strict: a value that does not fit the type exactly becomes NULL, which the
    `invalid_type` rule then sends to quarantine.

    Args:
        column: Column contract.
        alias: Alias of the raw relation.

    Returns:
        A SQL expression.
    """
    ref = f"{alias}.{q(column.name)}"
    match column.type:
        case "VARCHAR":
            return ref
        case "BIGINT":
            number = f"try_cast({ref} AS DOUBLE)"
            return f"CASE WHEN {number} = floor({number}) THEN {number}::BIGINT END"
        case "DECIMAL":
            return (
                f"CASE WHEN regexp_full_match({ref}, '-?[0-9]+([.][0-9]{{1,2}})?') "
                f"THEN {ref}::DECIMAL(18, 2) END"
            )
        case "DOUBLE":
            return f"try_cast({ref} AS DOUBLE)"
        case "DATE":
            return f"try_strptime({ref}, '%Y-%m-%d')::DATE"
        case "TIMESTAMP":
            return f"try_strptime({ref}, '%Y-%m-%d %H:%M:%S')"
        case "TIME":
            return f"try_strptime({ref}, '%H:%M:%S')::TIME"
        case "BOOLEAN":
            return f"CASE {ref} WHEN 'True' THEN true WHEN 'False' THEN false END"


def register_reference(con: duckdb.DuckDBPyConnection, norm: Normalization) -> None:
    """Loads the reference tables: home countries, coherent documents and value mappings.

    Args:
        con: DuckDB connection.
        norm: Normalization mapping.
    """
    con.execute(
        "CREATE OR REPLACE TABLE home_countries "
        "(country_name VARCHAR, country_code VARCHAR, timezone VARCHAR, currency VARCHAR)"
    )
    con.executemany(
        "INSERT INTO home_countries VALUES (?, ?, ?, ?)",
        [
            (name, code, norm.zones[code], norm.currencies[code])
            for name, code in sorted(norm.countries.items())
            if code in norm.zones
        ],
    )
    con.execute(
        "CREATE OR REPLACE TABLE country_documents (country_code VARCHAR, document_type VARCHAR)"
    )
    con.executemany(
        "INSERT INTO country_documents VALUES (?, ?)",
        [(code, doc) for code, docs in sorted(norm.documents.items()) for doc in docs],
    )
    con.execute(
        "CREATE OR REPLACE TABLE value_map "
        "(section VARCHAR, source_value VARCHAR, target_value VARCHAR)"
    )
    con.executemany(
        "INSERT INTO value_map VALUES (?, ?, ?)",
        [
            (section, source, target)
            for section in ("countries", "product_types")
            for source, target in sorted(norm.section(section).items())
        ],
    )


def zone_join(contract: Contract) -> str:
    """Returns the join that attaches the row's home `country_code`, `timezone` and `currency`.

    Args:
        contract: Table contract.

    Returns:
        A LEFT JOIN clause aliased `z`, or an empty string.
    """
    if contract.zone == "own_country":
        return "LEFT JOIN home_countries z ON z.country_name = r.country"
    if contract.zone == "customer":
        return (
            "LEFT JOIN (SELECT c.customer_id, c.country_code, c.timezone, h.currency "
            "FROM silver_customers c "
            "JOIN (SELECT DISTINCT country_code, currency FROM home_countries) h "
            "ON h.country_code = c.country_code) z ON z.customer_id = r.customer_id"
        )
    return ""


def _all_present(columns: tuple[str, ...], alias: str) -> str:
    return " AND ".join(f"{alias}.{q(c)} IS NOT NULL" for c in columns)


def reference_missing(ref: Reference, alias: str) -> str:
    """Returns SQL that is true when a present reference has no match in the target's silver.

    Args:
        ref: Reference rule.
        alias: Alias of the relation holding the referencing columns.

    Returns:
        A SQL boolean expression.
    """
    match = " AND ".join(
        f"x.{q(target)} = {alias}.{q(col)}"
        for col, target in zip(ref.columns, ref.target, strict=True)
    )
    return (
        f"{_all_present(ref.columns, alias)} AND NOT EXISTS "
        f"(SELECT 1 FROM {q('silver_' + ref.table)} x WHERE {match})"
    )


def mapped_joins(contract: Contract) -> str:
    """Returns one LEFT JOIN to `value_map` per mapped column, aliased `m_<column>`.

    Args:
        contract: Table contract.

    Returns:
        JOIN clauses, possibly empty.
    """
    return " ".join(
        f"LEFT JOIN value_map {q('m_' + c.name)} ON {q('m_' + c.name)}.section = "
        f"{sql_str(c.mapped_to)} AND {q('m_' + c.name)}.source_value = t.{q(c.name)}"
        for c in contract.columns
        if c.mapped_to
    )


def output_columns(contract: Contract) -> list[str]:
    """Returns the SELECT list of a silver row.

    Timestamps of zoned tables become TIMESTAMPTZ: the naive source value is read as local time
    in the row's zone. `event_date_local` is the local calendar day of the event. Mapped columns
    hold the normalized value and keep the source value in `<column>_source`.

    Args:
        contract: Table contract.

    Returns:
        SQL select items over aliases `t` (typed), `r` (raw), `z` (zone) and `m_<column>`.
    """
    items = []
    for column in contract.columns:
        expr = f"t.{q(column.name)}"
        if column.type == "TIMESTAMP" and contract.zone:
            expr = f"timezone(z.timezone, {expr})"
        if column.mapped_to:
            items.append(f"{expr} AS {q(column.name + '_source')}")
            expr = f"{q('m_' + column.name)}.target_value"
        items.append(f"{expr} AS {q(column.name)}")
    if contract.zone:
        items += ["z.country_code AS country_code", "z.timezone AS timezone"]
    if contract.event_column:
        items.append(f"t.{q(contract.event_column)}::DATE AS event_date_local")
    items += [f"coalesce({a.flagged}, false) AS {q('alert_' + a.name)}" for a in contract.alerts]
    items += [
        f"coalesce({reference_missing(ref, 't')}, false) AS {q('alert_' + ref.alert)}"
        for ref in contract.references
        if ref.alert
    ]
    if not contract.partitioned:
        keys = ", ".join(f"t.{q(k)}" for k in contract.business_key)
        items.append(
            f"coalesce({_all_present(contract.business_key, 't')} "
            f"AND count(*) OVER (PARTITION BY {keys}) > 1, false) AS alert_duplicate_business_key"
        )
    items += ["r.source_file AS source_file", "r.partition_date AS partition_date"]
    return items


def _rule(rule: str, column: str, value: str, where: str) -> str:
    return (
        f"SELECT r._rid, {sql_str(rule)}, {sql_str(column)}, {value} "
        f"FROM raw r JOIN typed t ON t._rid = r._rid WHERE coalesce({where}, false)"
    )


def _duplicates(rule: str, keys: tuple[str, ...], contract: Contract) -> str:
    """Flags every occurrence of a key after the first, in partition and file order."""
    partition = ", ".join(f"r.{q(k)}" for k in keys)
    tiebreak = ", ".join(f"r.{q(c)}" for c in contract.header)
    return f"""
        SELECT _rid, {sql_str(rule)}, {sql_str(",".join(keys))}, key_value FROM (
            SELECT r._rid, concat_ws('|', {partition}) AS key_value,
                   row_number() OVER (PARTITION BY {partition}
                                      ORDER BY r.partition_date, r.source_file, {tiebreak}) AS n
            FROM raw r WHERE {_all_present(keys, "r")})
        WHERE n > 1
    """


def violation_queries(contract: Contract) -> list[str]:
    """Returns one query per rule; each yields (_rid, rule, column_name, value) for violating rows.

    Args:
        contract: Table contract.

    Returns:
        SQL SELECT statements over `raw r` and `typed t`.
    """
    queries = []
    for column in contract.columns:
        name, ref, typed = column.name, f"r.{q(column.name)}", f"t.{q(column.name)}"
        if column.required:
            queries.append(_rule("missing_required", name, "NULL", f"{ref} IS NULL"))
        if column.type != "VARCHAR":
            queries.append(
                _rule("invalid_type", name, ref, f"{ref} IS NOT NULL AND {typed} IS NULL")
            )
        if column.domain:
            allowed = ", ".join(sql_str(v) for v in column.domain)
            queries.append(_rule("invalid_domain", name, ref, f"{typed} NOT IN ({allowed})"))
        if column.mapped_to:
            queries.append(
                _rule(
                    "unmapped_value",
                    name,
                    ref,
                    f"{typed} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM value_map m "
                    f"WHERE m.section = {sql_str(column.mapped_to)} AND m.source_value = {typed})",
                )
            )
    if contract.partitioned and "process_date" in contract.header:
        queries.append(
            _rule(
                "process_date_mismatch",
                "process_date",
                "r.process_date",
                "t.process_date <> r.partition_date",
            )
        )
    if contract.event_column and contract.cutoff_hours is not None:
        start, end = contract.cutoff_hours, contract.cutoff_hours + 24
        event = f"t.{q(contract.event_column)}"
        queries.append(
            _rule(
                "event_outside_partition",
                contract.event_column,
                f"r.{q(contract.event_column)}",
                f"{event} NOT BETWEEN r.partition_date + INTERVAL {start} HOUR "
                f"AND r.partition_date + INTERVAL {end} HOUR",
            )
        )
    queries.append(_duplicates("duplicate_key", contract.primary_key, contract))
    if contract.partitioned:
        queries.append(_duplicates("duplicate_business_key", contract.business_key, contract))
    for reference in contract.references:
        if not reference.alert:
            value = "concat_ws('|', " + ", ".join(f"r.{q(c)}" for c in reference.columns) + ")"
            queries.append(
                _rule(
                    "missing_reference",
                    ",".join(reference.columns),
                    value,
                    reference_missing(reference, "r"),
                )
            )
    for check in contract.checks:
        queries.append(_rule(check.rule, check.column, f"r.{q(check.column)}", check.violated))
    return queries


def register_silver_view(
    con: duckdb.DuckDBPyConnection, data_dir: Path, contract: Contract
) -> None:
    """Exposes a table's silver output as `silver_<table>` for lookups by later tables.

    Args:
        con: DuckDB connection.
        data_dir: Root data directory.
        contract: Table contract.
    """
    folder = silver_dir(data_dir, contract.table)
    name = q(f"silver_{contract.table}")
    if any(folder.rglob("*.parquet")):
        glob = folder / ("*/*.parquet" if contract.partitioned else "data.parquet")
        con.execute(
            f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet({sql_str(str(glob))})"
        )
    else:
        con.execute(f"DROP VIEW IF EXISTS {name}")
        con.execute(f"CREATE OR REPLACE TABLE {name} AS SELECT * FROM silver_out LIMIT 0")


def _write(
    con: duckdb.DuckDBPyConnection, relation: str, order: str, target: Path, partitioned: bool
) -> None:
    """Writes a relation to Parquet in a fixed order, so equal input gives byte-identical files."""
    shutil.rmtree(target, ignore_errors=True)
    rows = con.execute(f"SELECT count(*) FROM {relation}").fetchone()
    if not rows or rows[0] == 0:
        return
    target.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE ordered_out AS SELECT * FROM {relation} ORDER BY {order}"
    )
    # With several threads, sorted chunks reach each file in thread order, so row order and
    # therefore the file hash would change between runs.
    con.execute("SET threads = 1")
    try:
        if partitioned:
            con.execute(f"COPY ordered_out TO {sql_str(str(target))} ({PARTITIONED_PARQUET})")
        else:
            con.execute(f"COPY ordered_out TO {sql_str(str(target / 'data.parquet'))} ({PARQUET})")
    finally:
        con.execute("RESET threads")
        con.execute("DROP TABLE ordered_out")


def header_diff(expected: list[str], found: list[str]) -> str:
    """Describes how a file header differs from the contract.

    Args:
        expected: Contract header.
        found: File header.

    Returns:
        For example "added: channel_v2; missing: none", or "column order changed".
    """
    added = [c for c in found if c not in expected]
    missing = [c for c in expected if c not in found]
    if not added and not missing:
        return "column order changed"
    return f"added: {', '.join(added) or 'none'}; missing: {', '.join(missing) or 'none'}"


def _quarantine_file(
    con: duckdb.DuckDBPyConnection, data_dir: Path, contract: Contract, file: BronzeFile
) -> int:
    """Sends every row of a file whose header breaks the contract to quarantine.

    Returns:
        Rows quarantined.
    """
    keys = [k for k in contract.primary_key if k in file.header]
    record_key = (
        "concat_ws('|', " + ", ".join(q(k) for k in keys) + ")"
        if len(keys) == len(contract.primary_key)
        else "NULL"
    )
    partition = f"DATE {sql_str(file.partition_date)}" if file.partition_date else "NULL::DATE"
    detail = sql_str(header_diff(contract.header, file.header))
    before = con.execute("SELECT count(*) FROM quarantine_out").fetchone()
    con.execute(
        f"""
        INSERT INTO quarantine_out
        SELECT {sql_str(contract.table)}, {sql_str(file.path)}, {partition}, {record_key},
               [struct_pack(rule := 'schema_mismatch', column_name := NULL::VARCHAR,
                            value := {detail})]
        FROM read_csv({sql_str(str(data_dir / file.path))}, header = true, all_varchar = true,
                      delim = ',', quote = '"', escape = '"')
        """
    )
    after = con.execute("SELECT count(*) FROM quarantine_out").fetchone()
    return (after[0] if after else 0) - (before[0] if before else 0)


def build_table(
    con: duckdb.DuckDBPyConnection, data_dir: Path, contract: Contract, files: list[BronzeFile]
) -> TableResult:
    """Builds silver and quarantine for one table from its bronze files.

    Args:
        con: DuckDB connection with `home_countries` and the silver views of earlier tables.
        data_dir: Root data directory.
        contract: Table contract.
        files: Bronze files of this table.

    Returns:
        Row counts.

    Raises:
        ValueError: If silver plus quarantine rows do not add up to the bronze rows.
    """
    matching = [f for f in files if f.header == contract.header]
    mismatched = [f for f in files if f.header != contract.header]
    con.execute(
        "CREATE OR REPLACE TEMP TABLE bronze_files "
        "(path VARCHAR, abs_path VARCHAR, partition_date DATE)"
    )
    con.executemany(
        "INSERT INTO bronze_files VALUES (?, ?, ?)",
        [(f.path, str(data_dir / f.path), f.partition) for f in matching],
    )
    paths = "[" + ", ".join(sql_str(str(data_dir / f.path)) for f in matching) + "]"
    header_types = "{" + ", ".join(f"{sql_str(c)}: 'VARCHAR'" for c in contract.header) + "}"
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE raw AS
        SELECT row_number() OVER () AS _rid, f.path AS source_file, f.partition_date,
               r.* EXCLUDE (filename)
        FROM read_csv({paths}, header = true, delim = ',', quote = '"', escape = '"',
                      columns = {header_types}, filename = true, hive_partitioning = false) r
        JOIN bronze_files f ON f.abs_path = r.filename
        """
        if matching
        else f"""
        CREATE OR REPLACE TEMP TABLE raw AS
        SELECT NULL::BIGINT AS _rid, NULL::VARCHAR AS source_file, NULL::DATE AS partition_date,
               {", ".join(f"NULL::VARCHAR AS {q(c)}" for c in contract.header)}
        WHERE false
        """
    )
    parsed = ", ".join(f"{parse_expr(c)} AS {q(c.name)}" for c in contract.columns)
    con.execute(f"CREATE OR REPLACE TEMP TABLE typed AS SELECT r._rid, {parsed} FROM raw r")
    queries = violation_queries(contract)
    con.execute(
        "CREATE OR REPLACE TEMP TABLE violations "
        "(_rid BIGINT, rule VARCHAR, column_name VARCHAR, value VARCHAR)"
    )
    for query in queries:
        con.execute(f"INSERT INTO violations {query}")

    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE silver_out AS
        SELECT {", ".join(output_columns(contract))}
        FROM raw r JOIN typed t ON t._rid = r._rid {zone_join(contract)} {mapped_joins(contract)}
        WHERE NOT EXISTS (SELECT 1 FROM violations v WHERE v._rid = r._rid)
        """
    )
    record_key = "concat_ws('|', " + ", ".join(f"r.{q(k)}" for k in contract.primary_key) + ")"
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE quarantine_out AS
        SELECT {sql_str(contract.table)} AS table_name, r.source_file, r.partition_date,
               {record_key} AS record_key,
               list(struct_pack(rule := v.rule, column_name := v.column_name, value := v.value)
                    ORDER BY v.rule, v.column_name, v.value) AS violations
        FROM violations v JOIN raw r ON r._rid = v._rid
        GROUP BY r._rid, r.source_file, r.partition_date, record_key
        """
    )

    mismatched_rows = sum(_quarantine_file(con, data_dir, contract, f) for f in mismatched)

    pk = ", ".join(q(k) for k in contract.primary_key)
    _write(
        con,
        "silver_out",
        f"partition_date, {pk}, source_file",
        silver_dir(data_dir, contract.table),
        contract.partitioned,
    )
    _write(
        con,
        "quarantine_out",
        "source_file, record_key, violations",
        quarantine_dir(data_dir, contract.table),
        contract.partitioned,
    )
    register_silver_view(con, data_dir, contract)

    counts = con.execute(
        "SELECT (SELECT count(*) FROM raw), (SELECT count(*) FROM silver_out), "
        "(SELECT count(*) FROM quarantine_out)"
    ).fetchone()
    assert counts is not None
    result = TableResult(
        contract.table, len(files), counts[0] + mismatched_rows, counts[1], counts[2]
    )
    if result.bronze_rows != result.silver_rows + result.quarantine_rows:
        raise ValueError(f"{contract.table}: silver + quarantine rows do not equal bronze rows")
    log.info("silver_table", **result.__dict__)
    return result
