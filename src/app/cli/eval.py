"""Golden-case runner.

  python -m app.cli.eval eval/cases --out eval/reports

Each case is one YAML file: fixtures (initial DB state), then one or more chat turns, each
with the expected intent, outcome, autonomy level and actions. The runner:

  1. gives every case its own throwaway schema in the Postgres of DATABASE_URL, so cases are
     order-independent, never touch existing tables and do not depend on the synthetic dataset
     (whose timestamps move with the clock),
  2. loads the fixtures as the schema owner through the real ingestion validator (bad fixtures
     fail loudly),
  3. drives the conversation through the real API (POST /chat) in-process, connected as
     trazo_app so row level security applies,
  4. compares, and writes a JSON + Markdown report.

A case may declare `known_failure: <why>`. It still runs and shows up in the report, but does
not fail the run. If it starts passing, the run fails so the case gets promoted to a normal one.

A case may declare `now: <ISO datetime>`, the moment the customer writes. It becomes the
simulated clock of that case, and fixture timestamps (`hours_ago`) count back from it. Without
it, the case uses TRAZO_NOW. Fixture transactions default to the case customer and to the first
fixture product.

A case may declare `skip: <why>` when its expectations no longer apply and a later story will
rewrite it. It is not run, and it is listed in the report with its reason.

Exit code: 0 if every non-known-failure case passes, 1 otherwise.
"""

import argparse
import json
import os
import statistics
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from fastapi.testclient import TestClient

from app.adapters.db.session import Database, SchemaUrls, isolated_schema
from app.core.config import Settings
from app.core.logging import configure_logging, get_logger
from app.core.time import utcnow
from app.main import create_app
from app.services.ingestion import ingest_rows

log = get_logger("eval")

TURN_FIELDS = ("intent", "outcome", "autonomy_level", "actions_taken")


@dataclass
class TurnResult:
    """Expected and actual outcome of one chat turn."""

    message: str
    confirm: bool
    expected: dict[str, Any]
    actual: dict[str, Any]
    mismatches: list[str] = field(default_factory=list)


@dataclass
class CaseResult:
    """Outcome of one golden case."""

    id: str
    description: str
    tags: list[str]
    status: str  # pass | fail | known_failure | unexpected_pass | error | skipped
    known_failure: str | None = None
    skipped: str | None = None
    turns: list[TurnResult] = field(default_factory=list)
    error: str | None = None


# ---- loading ------------------------------------------------------------------------------


def load_cases(cases_dir: str | Path) -> list[dict[str, Any]]:
    """Reads every YAML case in a directory, sorted by file name.

    Args:
        cases_dir: Directory with one YAML file per case.

    Returns:
        The parsed cases.

    Raises:
        ValueError: If a case has no turns.
    """
    paths = sorted(Path(cases_dir).glob("*.yaml")) + sorted(Path(cases_dir).glob("*.yml"))
    cases = []
    for p in paths:
        with open(p, encoding="utf-8") as f:
            case = yaml.safe_load(f)
        case.setdefault("id", p.stem)
        if not case.get("turns"):
            raise ValueError(f"{p}: case has no turns")
        cases.append(case)
    return cases


def _fixture_rows(
    case: dict[str, Any], now: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    fx = case.get("fixtures") or {}
    customers = [dict(fx["customer"])] if fx.get("customer") else []
    customer_id = customers[0]["customer_id"] if customers else None
    products = [{"customer_id": customer_id, **p} for p in fx.get("products") or []]
    transactions: list[dict[str, Any]] = []
    for t in fx.get("transactions") or []:
        row = dict(t)
        row["transaction_date"] = now - timedelta(hours=float(row.pop("hours_ago", 1)))
        row["process_date"] = row["transaction_date"].date()
        row.setdefault("customer_id", customer_id)
        row.setdefault("product_id", products[0]["product_id"] if products else None)
        transactions.append(row)
    return customers, products, transactions


# ---- running ------------------------------------------------------------------------------


def run_case(case: dict[str, Any], urls: SchemaUrls) -> CaseResult:
    """Runs one case through the real API against the given database.

    Args:
        case: Parsed case.
        urls: Owner and trazo_app URLs of an empty, migrated schema reserved for this case.

    Returns:
        The case result.
    """
    result = CaseResult(
        id=case["id"],
        description=case.get("description", ""),
        tags=case.get("tags", []),
        status="pass",
        known_failure=case.get("known_failure"),
    )
    overrides = {"trazo_now": case["now"]} if "now" in case else {}
    settings = Settings(database_url=urls.app, **overrides)
    app = create_app(settings)
    rows = _fixture_rows(case, settings.trazo_now)

    owner = Database(urls.admin)
    try:
        with owner.session() as s:
            report = ingest_rows(s, "eval", settings.document_hash_key, *rows)
    finally:
        owner.dispose()
    if sum(report.rejected.values()):
        result.status = "error"
        result.error = f"fixtures rejected by validator: {dict(report.reasons)}"
        return result

    with TestClient(app) as client:
        customers = rows[0]
        customer_id = customers[0]["customer_id"] if customers else case["customer_id"]
        case_id = None
        for turn in case["turns"]:
            body = {
                "customer_id": customer_id,
                "message": turn["message"],
                "confirm": bool(turn.get("confirm", False)),
                "case_id": case_id if turn.get("same_case", True) else None,
            }
            r = client.post("/chat", json=body)
            if r.status_code != 200:
                result.status = "error"
                result.error = f"POST /chat returned {r.status_code}: {r.text[:200]}"
                return result
            out = r.json()
            case_id = out["case_id"]
            if "escalation_reason_contains" in turn["expect"]:
                out["escalation_reason"] = (
                    client.get(f"/cases/{case_id}").json().get("escalation_reason")
                )
            result.turns.append(_compare(turn, out))

    failed = any(t.mismatches for t in result.turns)
    if result.known_failure:
        result.status = "unexpected_pass" if not failed else "known_failure"
    elif failed:
        result.status = "fail"
    return result


def _compare(turn: dict[str, Any], out: dict[str, Any]) -> TurnResult:
    exp = turn["expect"]
    tr = TurnResult(
        message=turn["message"],
        confirm=bool(turn.get("confirm", False)),
        expected=exp,
        actual={
            k: out.get(k)
            for k in (
                *TURN_FIELDS,
                "llm_fallback",
                "tokens",
                "latency_ms",
                "escalation_reason",
                "reply",
            )
        },
    )
    for k in TURN_FIELDS:
        if k in exp and out.get(k) != exp[k]:
            tr.mismatches.append(f"{k}: expected {exp[k]!r}, got {out.get(k)!r}")
    needle = exp.get("escalation_reason_contains")
    if needle and needle not in (out.get("escalation_reason") or ""):
        tr.mismatches.append(
            f"escalation_reason: expected to contain {needle!r}, "
            f"got {out.get('escalation_reason')!r}"
        )
    return tr


# ---- reporting ----------------------------------------------------------------------------


def summarize(results: list[CaseResult]) -> dict[str, Any]:
    """Aggregates pass counts, escalation quality, latency and tokens.

    Args:
        results: Case results.

    Returns:
        The summary.
    """
    turns = [t for r in results for t in r.turns]
    graded = [r for r in results if not r.known_failure and not r.skipped]
    lat = [t.actual["latency_ms"] for t in turns if t.actual.get("latency_ms") is not None]

    def escalated(d: dict[str, Any]) -> bool:
        return d.get("outcome") == "escalated"

    # Escalation quality is measured on graded cases only; known failures would skew it.
    with_outcome = [t for r in graded for t in r.turns if "outcome" in t.expected]
    return {
        "cases": len(results),
        "passed": sum(r.status == "pass" for r in results),
        "failed": sum(r.status in ("fail", "error") for r in graded),
        "known_failures": sum(r.status == "known_failure" for r in results),
        "unexpected_passes": sum(r.status == "unexpected_pass" for r in results),
        "skipped": sum(r.status == "skipped" for r in results),
        "turns": len(turns),
        "escalation": {
            "correct": sum(escalated(t.expected) and escalated(t.actual) for t in with_outcome),
            "missed": sum(escalated(t.expected) and not escalated(t.actual) for t in with_outcome),
            "over": sum(not escalated(t.expected) and escalated(t.actual) for t in with_outcome),
        },
        "latency_ms": {
            "mean": round(statistics.fmean(lat), 1) if lat else None,
            "p95": _p95(lat),
        },
        "tokens_total": sum(t.actual.get("tokens") or 0 for t in turns),
        "tokens_per_case": round(
            sum(t.actual.get("tokens") or 0 for t in turns) / max(len(results), 1), 1
        ),
        "llm_fallback_turns": sum(bool(t.actual.get("llm_fallback")) for t in turns),
    }


def _p95(xs: list[int]) -> int | None:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]


def write_report(results: list[CaseResult], summary: dict[str, Any], out_dir: str | Path) -> Path:
    """Writes golden_report.json and golden_report.md.

    Args:
        results: Case results.
        summary: Output of summarize.
        out_dir: Directory for the reports; created if missing.

    Returns:
        Path of the Markdown report.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    settings = Settings()
    meta = {
        "generated_at": utcnow().isoformat(timespec="seconds") + "Z",
        "llm_enabled": os.getenv("LLM_ENABLED", "true"),
        "llm_provider": settings.llm_provider,
        "llm_model": settings.llm_model_primary,
    }
    payload = {"meta": meta, "summary": summary, "cases": [asdict(r) for r in results]}
    (out / "golden_report.json").write_text(json.dumps(payload, indent=2, default=str))

    lines = [
        "# Golden cases report",
        "",
        f"Generated {meta['generated_at']} · LLM enabled: {meta['llm_enabled']} · "
        f"provider {meta['llm_provider']} · model {meta['llm_model']}",
        "",
        f"**{summary['passed']} / {summary['cases']} passed** · "
        f"{summary['failed']} failed · {summary['known_failures']} known failures · "
        f"{summary['unexpected_passes']} unexpected passes · {summary['skipped']} skipped",
        "",
        f"Escalations: {summary['escalation']['correct']} correct, "
        f"{summary['escalation']['missed']} missed, {summary['escalation']['over']} over-escalated",
        "",
        f"Latency per turn: mean {summary['latency_ms']['mean']} ms, "
        f"p95 {summary['latency_ms']['p95']} ms · tokens per case {summary['tokens_per_case']} · "
        f"LLM fallback turns {summary['llm_fallback_turns']}/{summary['turns']}",
        "",
        "| Case | Status | Tags | Detail |",
        "| --- | --- | --- | --- |",
    ]
    for r in results:
        detail = r.error or "; ".join(m for t in r.turns for m in t.mismatches)
        if r.known_failure:
            detail = f"known: {r.known_failure}" + (f" ({detail})" if detail else "")
        if r.skipped:
            detail = f"skipped: {r.skipped}"
        lines.append(f"| {r.id} | {r.status} | {', '.join(r.tags)} | {detail} |")
    path = out / "golden_report.md"
    path.write_text("\n".join(lines) + "\n")
    return path


# ---- entry --------------------------------------------------------------------------------


def run(cases_dir: str | Path, out_dir: str | Path) -> tuple[list[CaseResult], dict[str, Any]]:
    """Runs every case in a directory and writes the reports.

    Args:
        cases_dir: Directory with one YAML file per case.
        out_dir: Directory for the reports.

    Returns:
        The case results and the summary.
    """
    cases = load_cases(cases_dir)
    if not cases:
        raise SystemExit(f"no cases found in {cases_dir}")
    base = Settings()
    if not base.admin_database_url:
        raise SystemExit("ADMIN_DATABASE_URL is not set")
    results = []
    for case in cases:
        if case.get("skip"):
            results.append(
                CaseResult(
                    id=case["id"],
                    description=case.get("description", ""),
                    tags=case.get("tags", []),
                    status="skipped",
                    skipped=case["skip"],
                )
            )
            continue
        try:
            with isolated_schema(base.admin_database_url, base.database_url, "golden") as urls:
                results.append(run_case(case, urls))
        except Exception as e:  # a crash in one case must not hide the others
            results.append(
                CaseResult(
                    id=case["id"],
                    description=case.get("description", ""),
                    tags=case.get("tags", []),
                    status="error",
                    known_failure=case.get("known_failure"),
                    error=f"{type(e).__name__}: {e}",
                )
            )
    summary = summarize(results)
    write_report(results, summary, out_dir)
    return results, summary


def main(argv: list[str] | None = None) -> int:
    """Runs the golden cases and logs one line per case plus the summary.

    Args:
        argv: Command-line arguments; defaults to sys.argv.

    Returns:
        0 if every graded case passes and no known failure started passing, else 1.
    """
    ap = argparse.ArgumentParser(prog="eval")
    ap.add_argument("cases_dir", help="directory with one YAML file per case")
    ap.add_argument("--out", default="eval/reports", help="where to write the report")
    args = ap.parse_args(argv)

    results, summary = run(args.cases_dir, args.out)

    # The app logs of every case stay at the configured level; the verdict is always shown.
    configure_logging("INFO")
    for r in results:
        mismatches = [m for t in r.turns for m in t.mismatches]
        log.info("case_result", case=r.id, status=r.status, mismatches=mismatches, error=r.error)
    log.info("summary", report=str(Path(args.out) / "golden_report.md"), **summary)

    ok = summary["failed"] == 0 and summary["unexpected_passes"] == 0
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
