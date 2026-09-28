"""`make init` (TRZ-07): creates .env, fills the placeholder secrets once, keeps what the user set
and never shows a secret."""

import logging
from pathlib import Path

import pytest

from app.cli import init_env as init_cli
from app.cli import seed as seed_cli
from app.cli.init_env import _values, init_env

EXAMPLE = Path(".env.example")


@pytest.fixture
def env(tmp_path: Path) -> Path:
    return tmp_path / ".env"


def test_creates_env_and_generates_every_secret(env: Path) -> None:
    changed = init_env(EXAMPLE, env)
    values, example = _values(env.read_text()), _values(EXAMPLE.read_text())

    assert sorted(changed) == [
        "ANALYST_DEMO_PASSWORD",
        "APP_DB_PASSWORD",
        "DATABASE_URL",
        "DOCUMENT_HASH_KEY",
        "JWT_SECRET",
    ]
    assert len(values["DOCUMENT_HASH_KEY"]) == 48
    # Long enough for the API to accept it as the session signing key.
    assert len(values["JWT_SECRET"]) >= 32
    assert values["ANALYST_DEMO_PASSWORD"] != example["ANALYST_DEMO_PASSWORD"]
    assert values["APP_DB_PASSWORD"] != example["APP_DB_PASSWORD"]
    # The URL the migration reads the role password from follows the new password.
    assert f"trazo_app:{values['APP_DB_PASSWORD']}@" in values["DATABASE_URL"]
    # Everything else is the example, line for line.
    assert len(env.read_text().splitlines()) == len(EXAMPLE.read_text().splitlines())
    assert {k: v for k, v in values.items() if k not in changed} == {
        k: v for k, v in example.items() if k not in changed
    }


def test_second_run_changes_nothing(env: Path) -> None:
    init_env(EXAMPLE, env)
    before = env.read_text()
    assert init_env(EXAMPLE, env) == []
    assert env.read_text() == before


def test_values_the_user_set_are_kept(env: Path) -> None:
    text = EXAMPLE.read_text()
    text = text.replace("APP_DB_PASSWORD=cambia-esto-tambien", "APP_DB_PASSWORD=mine")
    text = text.replace("trazo_app:cambia-esto-tambien@", "trazo_app:mine@")
    text = text.replace("JWT_SECRET=genera-uno-largo-y-aleatorio", "JWT_SECRET=my-secret")
    text = text.replace("ANALYST_DEMO_PASSWORD=cambia-esto", "ANALYST_DEMO_PASSWORD=my-pass")
    env.write_text(text.replace("DOCUMENT_HASH_KEY=", "DOCUMENT_HASH_KEY=my-key"))

    assert init_env(EXAMPLE, env) == []
    values = _values(env.read_text())
    assert values["APP_DB_PASSWORD"] == "mine"
    assert values["DOCUMENT_HASH_KEY"] == "my-key"
    assert values["JWT_SECRET"] == "my-secret"
    assert values["ANALYST_DEMO_PASSWORD"] == "my-pass"


def test_missing_variables_are_appended_to_an_older_env(env: Path) -> None:
    env.write_text("POSTGRES_USER=trazo")
    changed = init_env(EXAMPLE, env)
    values = _values(env.read_text())

    assert sorted(changed) == [
        "ANALYST_DEMO_PASSWORD",
        "APP_DB_PASSWORD",
        "DOCUMENT_HASH_KEY",
        "JWT_SECRET",
    ]
    assert values["POSTGRES_USER"] == "trazo"
    assert values["DOCUMENT_HASH_KEY"] and values["APP_DB_PASSWORD"]


def test_output_names_the_variables_but_never_their_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / ".env.example").write_text(EXAMPLE.read_text())
    monkeypatch.chdir(tmp_path)
    with caplog.at_level(logging.INFO, logger="init"):
        init_cli.main()
    values = _values((tmp_path / ".env").read_text())

    assert "DOCUMENT_HASH_KEY" in caplog.text
    assert values["DOCUMENT_HASH_KEY"] not in caplog.text
    assert values["APP_DB_PASSWORD"] not in caplog.text
    assert values["JWT_SECRET"] not in caplog.text
    assert values["ANALYST_DEMO_PASSWORD"] not in caplog.text


def test_seed_without_document_key_asks_for_make_init(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADMIN_DATABASE_URL", "postgresql+psycopg://unused@localhost:1/unused")
    monkeypatch.setenv("DOCUMENT_HASH_KEY", "")
    # Stops before connecting, so no database is needed.
    with pytest.raises(SystemExit, match="run make init"):
        seed_cli.main(["synthetic"])
