"""`make init`: creates .env from .env.example and fills the secrets that cannot keep the example.

DOCUMENT_HASH_KEY, APP_DB_PASSWORD, JWT_SECRET and ANALYST_DEMO_PASSWORD get a random value
when they are missing, empty or still equal to the example. The trazo_app password also lives
inside DATABASE_URL, which is where `make migrate` reads it from, so the URL follows the new
password while it still holds the old one; otherwise the API on the host and the API in compose
would expect different passwords.
Values the user already set are never touched, and no secret is written to the output.

Standard library only, and Python 3.9 syntax, so it runs before `uv sync` and on a machine that
only has Docker and the system Python.

  python3 src/app/cli/init_env.py
"""

from __future__ import annotations

import logging
import secrets
import sys
from pathlib import Path

GENERATED = ("DOCUMENT_HASH_KEY", "APP_DB_PASSWORD", "JWT_SECRET", "ANALYST_DEMO_PASSWORD")
APP_ROLE = "trazo_app"

log = logging.getLogger("init")


def _values(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        name, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            out[name.strip()] = value.strip()
    return out


def init_env(example: Path, target: Path) -> list[str]:
    """Creates target from example if missing and generates the placeholder secrets.

    Args:
        example: The .env.example file.
        target: The .env file.

    Returns:
        Names of the variables that received a new value; empty when nothing changed.
    """
    if not target.exists():
        target.write_text(example.read_text())
        log.info("init: created %s from %s", target.name, example.name)
    defaults = _values(example.read_text())
    current = _values(target.read_text())
    new = {}
    for name in GENERATED:
        value = current.get(name, "")
        if value in ("", defaults.get(name)):
            # Hex keeps the password safe inside a URL without escaping.
            new[name] = secrets.token_hex(24)
    if "APP_DB_PASSWORD" in new:
        url = current.get("DATABASE_URL", "")
        for old in {current.get("APP_DB_PASSWORD", ""), defaults.get("APP_DB_PASSWORD", "")}:
            if f"{APP_ROLE}:{old}@" in url:
                new["DATABASE_URL"] = url.replace(
                    f"{APP_ROLE}:{old}@", f"{APP_ROLE}:{new['APP_DB_PASSWORD']}@"
                )
                break

    lines = target.read_text().splitlines(keepends=True)
    pending = dict(new)
    for i, line in enumerate(lines):
        name = line.partition("=")[0].strip()
        if name in pending and not line.lstrip().startswith("#"):
            lines[i] = f"{name}={pending.pop(name)}\n"
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines += [f"{name}={value}\n" for name, value in pending.items()]
    target.write_text("".join(lines))
    return list(new)


def main() -> int:
    """Runs init on the .env of the current directory.

    Returns:
        Process exit code: 0 on success.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    changed = init_env(Path(".env.example"), Path(".env"))
    if changed:
        log.info("init: generated %s in .env (values not shown)", ", ".join(changed))
    else:
        log.info("init: .env already has its secrets; nothing changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
