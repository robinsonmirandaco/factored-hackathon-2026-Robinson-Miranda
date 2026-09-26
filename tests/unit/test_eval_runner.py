"""Golden-case files must stay consistent with the backlog."""

import re
from pathlib import Path

from app.cli.eval import load_cases

CASES = Path(__file__).resolve().parents[2] / "eval" / "cases"
STORY_ID = re.compile(r"\bTRZ-\d{2}\b")


def test_every_skipped_case_names_the_story_that_rewrites_it():
    # A skip without an owning story would hide a regression with no one to restore it.
    orphans = [
        c["id"] for c in load_cases(CASES) if "skip" in c and not STORY_ID.search(str(c["skip"]))
    ]
    assert orphans == []
