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
from pipeline.contracts import Column, Contract
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
        home_countries: ISO code to `zone` and `currency` for customer home countries.
    """

    version: int
    countries: dict[str, str]
    home_countries: dict[str, dict[str, str]]


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
    return Normalization(
        version=int(data["version"]),
        countries=dict(data["countries"]),
        home_countries={k: dict(v) for k, v in data["home_countries"].items()},
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
    """Loads the home-country table used to resolve time zones.

    Args:
        con: DuckDB connection.
        norm: Normalization mapping.
    """
    con.execute(
        "CREATE OR REPLACE TABLE home_countries "
        "(country_name VARCHAR, country_code VARCHAR, timezone VARCHAR, currency VARCHAR)"
    )
    rows = [
        (name, code, norm.home_countries[code]["zone"], norm.home_countries[code]["currency"])
        for name, code in sorted(norm.countries.items())
        if code in norm.home_countries
    ]
    con.executemany("INSERT INTO home_countries VALUES (?, ?, ?, ?)", rows)


def zone_join(contract: Contract) -> str:
    """Returns the join that attaches `country_code` and `timezone` to each raw row, if any.

    Args:
        contract: Table contract.

    Returns:
        A LEFT JOIN clause aliased `z`, or an empty string.
    """
    if contract.zone == "own_country":
        return "LEFT JOIN home_countries z ON z.country_name = r.country"
    if contract.zone == "customer":
        return (
            "LEFT JOIN (SELECT customer_id, country_code, timezone FROM silver_customers) z "
            "ON z.customer_id = r.customer_id"
        )
    return ""


def output_columns(contract: Contract) -> list[str]:
    """Returns the SELECT list of a silver row.

    Timestamps of zoned tables become TIMESTAMPTZ: the naive source value is read as local time
    in the row's zone. `event_date_local` is the local calendar day of the event.

    Args:
        contract: Table contract.

    Returns:
        SQL select items over aliases `t` (typed), `r` (raw) and `z` (zone).
    """
    items = []
    for column in contract.columns:
        expr = f"t.{q(column.name)}"
        if column.type == "TIMESTAMP" and contract.zone:
            expr = f"timezone(z.timezone, {expr})"
        items.append(f"{expr} AS {q(column.name)}")
    if contract.zone:
        items += ["z.country_code AS country_code", "z.timezone AS timezone"]
    if contract.event_column:
        items.append(f"t.{q(contract.event_column)}::DATE AS event_date_local")
    items += ["r.source_file AS source_file", "r.partition_date AS partition_date"]
    return items


def violation_queries(contract: Contract) -> list[str]:
    """Returns one query per rule; each yields (_rid, rule, column_name, value) for violating rows.

    Args:
        contract: Table contract.

    Returns:
        SQL SELECT statements over `raw r` and `typed t`.
    """
    queries = []
    for column in contract.columns:
        name, ref = sql_str(column.name), f"r.{q(column.name)}"
        if column.required:
            queries.append(
                f"SELECT r._rid, 'missing_required', {name}, NULL FROM raw r WHERE {ref} IS NULL"
            )
        if column.type != "VARCHAR":
            queries.append(
                f"SELECT r._rid, 'invalid_type', {name}, {ref} FROM raw r "
                f"JOIN typed t ON t._rid = r._rid "
                f"WHERE {ref} IS NOT NULL AND t.{q(column.name)} IS NULL"
            )
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
    con.execute(
        "CREATE OR REPLACE TEMP TABLE bronze_files "
        "(path VARCHAR, abs_path VARCHAR, partition_date DATE)"
    )
    con.executemany(
        "INSERT INTO bronze_files VALUES (?, ?, ?)",
        [(f.path, str(data_dir / f.path), f.partition) for f in files],
    )
    paths = "[" + ", ".join(sql_str(str(data_dir / f.path)) for f in files) + "]"
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
        if files
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
        FROM raw r JOIN typed t ON t._rid = r._rid {zone_join(contract)}
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
    result = TableResult(contract.table, len(files), counts[0], counts[1], counts[2])
    if result.bronze_rows != result.silver_rows + result.quarantine_rows:
        raise ValueError(f"{contract.table}: silver + quarantine rows do not equal bronze rows")
    log.info("silver_table", **result.__dict__)
    return result
