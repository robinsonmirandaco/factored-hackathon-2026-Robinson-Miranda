"""Logging in from tests through the real authentication endpoints (TRZ-09).

Customers built with `tests.serving_data.customer` have document CC `<customer_id>-DOC`. The
integration conftest turns demo mode on, so the fixed demo code logs them in.
"""

from fastapi.testclient import TestClient

TEST_JWT_SECRET = "test-only-jwt-secret-of-at-least-32-characters"
TEST_ANALYST_PASSWORD = "test-only-analyst-password"
DEMO_CODE = "482913"


def document(customer_id: str) -> dict[str, str]:
    """The identity document of a test customer."""
    return {"document_type": "CC", "document_number": f"{customer_id}-DOC"}


def customer_token(client: TestClient, customer_id: str) -> str:
    """Logs a test customer in with the demo code and returns the session token."""
    doc = document(customer_id)
    assert client.post("/auth/otp/request", json=doc).status_code == 202
    r = client.post("/auth/otp/verify", json={**doc, "code": DEMO_CODE})
    assert r.status_code == 200, r.text
    return str(r.json()["access_token"])


def customer_headers(client: TestClient, customer_id: str) -> dict[str, str]:
    """Authorization header of a new session of a test customer."""
    return {"authorization": f"Bearer {customer_token(client, customer_id)}"}


def analyst_headers(client: TestClient) -> dict[str, str]:
    """Authorization header of a new session of the demo analyst."""
    r = client.post(
        "/auth/analyst/login",
        json={"username": "analista.demo", "password": TEST_ANALYST_PASSWORD},
    )
    assert r.status_code == 200, r.text
    return {"authorization": f"Bearer {r.json()['access_token']}"}
