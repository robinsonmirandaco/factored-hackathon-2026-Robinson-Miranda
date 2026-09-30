"""The static web (TRZ-34): pages the content security policy can serve as they are."""

import re
from pathlib import Path

import pytest

PAGES = sorted(Path("web").glob("*.html"))
I18N = Path("web/assets/i18n.js")
SRC = Path("src")
# An inline handler such as onclick="..." would need 'unsafe-inline' to run.
INLINE_HANDLER = re.compile(r"<[^>]*\son[a-z]+\s*=", re.IGNORECASE)
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>", re.IGNORECASE)


def test_there_is_a_page() -> None:
    assert PAGES


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_a_page_has_no_inline_script_handler_or_style(page: Path) -> None:
    html = page.read_text(encoding="utf-8")
    assert not INLINE_SCRIPT.search(html)
    assert not INLINE_HANDLER.search(html)
    assert "style=" not in html and "<style" not in html


@pytest.mark.parametrize("page", PAGES, ids=lambda p: p.name)
def test_a_page_loads_nothing_from_another_origin(page: Path) -> None:
    html = page.read_text(encoding="utf-8")
    assert not re.search(r"(src|href)=\"(https?:)?//", html)


def _error_codes() -> set[str]:
    """Every error_code the API can send: AppError codes and the handlers' own."""
    codes: set[str] = set()
    for path in SRC.rglob("*.py"):
        code = path.read_text(encoding="utf-8")
        codes |= set(re.findall(r'AppError\(\s*"([a-z_]+)"', code))
        codes |= set(re.findall(r'error_response\(\s*\d+,\s*"([a-z_]+)"', code))
    return codes


def test_every_api_error_has_a_message_in_both_languages() -> None:
    i18n = I18N.read_text(encoding="utf-8")
    es, pt = i18n.split("\n  pt: {", 1)
    codes = _error_codes()
    assert len(codes) >= 10
    missing = sorted(c for c in codes if f"error_{c}:" not in es or f"error_{c}:" not in pt)
    assert not missing, missing
