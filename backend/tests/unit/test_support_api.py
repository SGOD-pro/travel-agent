"""Unit tests for the support inquiry API route."""

from fastapi.testclient import TestClient

from travel.runtime.fastapi.app import app

client = TestClient(app)


def test_submit_contact_inquiry_success() -> None:
    payload = {
        "name": "Devi Rao",
        "email": "devi@example.com",
        "inquiry_type": "Bespoke Corridor Curation",
        "corridor": "Western Ghats",
        "message": "Planning a driving trip from Bengaluru to Wayanad with 2 adults.",
    }
    response = client.post("/api/v1/support/inquiries", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert "inquiry_id" in data
    assert data["status"] == "received"
    assert "within 24–48 business hours" in data["message"]


def test_submit_contact_inquiry_validation_failure() -> None:
    # Missing required message
    payload = {
        "name": "Devi Rao",
        "email": "invalid-email",
    }
    response = client.post("/api/v1/support/inquiries", json=payload)
    assert response.status_code == 422
