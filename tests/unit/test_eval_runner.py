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


def test_no_golden_case_is_skipped_or_a_known_failure():
    # TRZ-17 CA8: every case is rewritten for the intents and the policy of design 8; TRZ-16
    # adds the recognition step and cases 23 to 25; TRZ-22 the claim status cases 26 to 29;
    # TRZ-23 case 30.
    cases = load_cases(CASES)
    assert len(cases) == 30
    assert [c["id"] for c in cases if "skip" in c or "known_failure" in c] == []
