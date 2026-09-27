"""The keyed hash that replaces identity document numbers (TRZ-07)."""

import hashlib
import hmac

import pytest

from app.domain.pii import document_hash


def test_is_hmac_sha256_of_type_and_number() -> None:
    expected = hmac.new(b"k", b"CC:123456", hashlib.sha256).hexdigest()
    assert document_hash("k", "CC", "123456") == expected


def test_same_document_typed_differently_gives_the_same_hash() -> None:
    assert document_hash("k", " cc", "123456 ") == document_hash("k", "CC", "123456")


def test_depends_on_the_key_and_never_contains_the_number() -> None:
    a, b = document_hash("k1", "DNI", "30111222"), document_hash("k2", "DNI", "30111222")
    assert a != b
    assert "30111222" not in a


def test_refuses_an_empty_key() -> None:
    with pytest.raises(ValueError, match="DOCUMENT_HASH_KEY"):
        document_hash("", "CC", "123456")
