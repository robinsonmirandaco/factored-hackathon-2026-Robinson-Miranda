"""The comparison behind the read-back after acting (TRZ-19 CA1)."""

from app.services.verification import mismatches


def test_equal_rows_have_no_mismatch() -> None:
    assert (
        mismatches({"status": "opened", "amount": 120.0}, {"status": "opened", "amount": 120.0})
        == ()
    )


def test_a_missing_row_is_named_missing() -> None:
    assert mismatches({"status": "opened"}, None) == ("missing",)


def test_every_differing_field_is_named_in_order() -> None:
    expected = {"amount": 120.0, "currency": "USD", "status": "opened"}
    found = {"amount": 12.0, "currency": "USD", "status": "closed"}
    assert mismatches(expected, found) == ("amount", "status")
