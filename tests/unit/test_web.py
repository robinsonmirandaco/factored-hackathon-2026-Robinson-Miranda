"""The static web (TRZ-34): pages the content security policy can serve as they are."""

import re
from pathlib import Path

import pytest

PAGES = sorted(Path("web").glob("*.html"))
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
