"""Prepares the demo state on a database that holds the cohort (TRZ-38).

  python -m app.cli.seed_demo        # make seed-demo

Runs after `make seed`. As the schema owner (ADMIN_DATABASE_URL) it creates the demo tables and
the reset function of db/demo/demo.sql, empties the operational tables, chooses the demo people
by the rules of config/demo.yaml, leaving out every customer of an evaluation case, and writes
the keyed hash of their invented documents. Then, as trazo_app (DATABASE_URL), it creates the
starting cases through the agent, as the reset endpoint does. It never reads a split file.
"""

import uuid
from pathlib import Path

from sqlalchemy import text

from app.adapters.db.session import Database, bind_context
from app.core.config import Settings
from app.core.errors import AppError
from app.core.logging import configure_logging, get_logger, new_trace_id
from app.domain.demo import load_demo
from app.main import build_runtime
from app.services import demo, seeding

log = get_logger("seed_demo")

MANIFEST = Path("eval/splits/manifest.json")


def main() -> int:
    """Seeds the demo state.

    Returns:
        Process exit code: 0 on success.
    """
    settings = Settings()
    configure_logging(settings.log_level)
    if not settings.admin_database_url:
        raise SystemExit("ADMIN_DATABASE_URL is not set")
    if not settings.document_hash_key:
        raise SystemExit("DOCUMENT_HASH_KEY is not set")
    new_trace_id()
    runtime = build_runtime(settings)
    config = load_demo(settings.demo_config_path)
    config.check_reasons(runtime.agent.policy.config.autonomy.reversal_reasons)
    deps = demo.offline(runtime.agent, settings)
    owner = Database(settings.admin_database_url)
    try:
        excluded = demo.read_excluded(Path(settings.data_dir) / "eval", MANIFEST)
        with owner.session() as s:
            source = seeding.current_source(s)
            if source != "cohort":
                raise demo.DemoError(f"the demo needs the cohort, and this database holds {source}")
            demo.install(s)
            # Rehearsals need a clean start: no open case of an earlier demo on any charge.
            bind_context(s, role="analyst")
            s.execute(text("SELECT demo_reset()"))
        with owner.session() as s:
            picks = demo.choose(s, runtime.db, deps, config, excluded)
            demo.assign_documents(s, settings.document_hash_key, config, picks)
            demo.save_roles(s, picks)
        out = demo.reset(runtime.db, deps, config, config.seed_analyst, f"seed-demo:{uuid.uuid4()}")
    except (demo.DemoError, AppError) as e:
        raise SystemExit(str(e)) from e
    finally:
        owner.dispose()
        runtime.db.dispose()
    log.info(
        "seed_demo_done",
        roles=len({p.role for p in picks}),
        excluded=len(excluded),
        **out.model_dump(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
