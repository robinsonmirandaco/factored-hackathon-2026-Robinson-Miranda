"""The static web (TRZ-34): pages the content security policy can serve as they are."""

import re
from pathlib import Path

import pytest

PAGES = sorted(Path("web").rglob("*.html"))
STYLES = sorted(Path("web/assets").glob("*.css"))
FONTS = Path("web/assets/fonts")
I18N = Path("web/assets/i18n.js")
SRC = Path("src")
# An inline handler such as onclick="..." would need 'unsafe-inline' to run.
INLINE_HANDLER = re.compile(r"<[^>]*\son[a-z]+\s*=", re.IGNORECASE)
INLINE_SCRIPT = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>", re.IGNORECASE)


def test_there_is_a_page() -> None:
    assert PAGES


@pytest.mark.parametrize("page", PAGES, ids=str)
def test_a_page_has_no_inline_script_handler_or_style(page: Path) -> None:
    html = page.read_text(encoding="utf-8")
    assert not INLINE_SCRIPT.search(html)
    assert not INLINE_HANDLER.search(html)
    assert "style=" not in html and "<style" not in html


@pytest.mark.parametrize("page", PAGES, ids=str)
def test_a_page_loads_nothing_from_another_origin(page: Path) -> None:
    html = page.read_text(encoding="utf-8")
    assert not re.search(r"(src|href)=\"(https?:)?//", html)


def test_the_customer_web_and_the_console_share_the_base_styles() -> None:
    for page in PAGES:
        assert 'href="/assets/base.css"' in page.read_text(encoding="utf-8"), page


@pytest.mark.parametrize("sheet", STYLES, ids=lambda p: p.name)
def test_a_stylesheet_loads_its_files_from_this_origin(sheet: Path) -> None:
    css = sheet.read_text(encoding="utf-8")
    assert "@import" not in css
    for url in re.findall(r"url\(\"?([^\")]+)\"?\)", css):
        assert not re.match(r"([a-z]+:)?//", url), url
        assert (sheet.parent / url).is_file(), url


@pytest.mark.parametrize("page", PAGES, ids=str)
def test_a_page_declares_the_icon_of_this_origin(page: Path) -> None:
    html = page.read_text(encoding="utf-8")
    assert '<link rel="icon" href="/assets/favicon.svg" type="image/svg+xml">' in html
    assert Path("web/assets/favicon.svg").is_file()


def test_the_fonts_travel_with_their_license() -> None:
    fonts = sorted(f.name for f in FONTS.glob("*.woff2"))
    assert fonts == ["Geist-Variable.woff2", "GeistMono-Variable.woff2"]
    assert "SIL Open Font License" in (FONTS / "OFL.txt").read_text(encoding="utf-8")


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


def test_the_message_field_leaves_no_room_for_the_browsers_own_warning() -> None:
    # A required field makes the browser show its own message in its own language ("Please
    # fill out this field."); an empty message is prevented by a disabled Send instead.
    html = Path("web/index.html").read_text(encoding="utf-8")
    field = re.search(r'<input id="message"[^>]*>', html)
    send = re.search(r'<button[^>]*id="send"[^>]*>', html)
    assert field and "required" not in field.group(0)
    assert send and "disabled" in send.group(0)


def _block(path: str, start: str, end: str) -> str:
    code = Path(path).read_text(encoding="utf-8")
    i = code.index(start)
    return code[i : code.index(end, i)]


def test_the_trace_and_the_history_number_each_step_once() -> None:
    # QA of the structured trace: a numbered list added its own number, restarting at each
    # turn, next to the number of the step ("1. 6."). The steps are a plain list with roles.
    blocks = [
        _block("web/assets/customer.js", "function timeline(", "\nconst traceOf"),
        _block("web/assets/analyst.js", "historyTurns(history).map(", "return cards;"),
    ]
    for block in blocks:
        assert 'el("ol"' not in block and 'el("li"' not in block
        assert 'role: "list"' in block and 'role: "listitem"' in block
        assert "step-no" in block
