from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def test_liveness_and_openapi_without_database() -> None:
    settings = Settings(
        _env_file=None,
        database_password="unit_test_only",
        database_host="127.0.0.1",
        database_port=1,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/health/live")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
        assert client.get("/docs").status_code == 200
        paths = client.get("/openapi.json").json()["paths"]
        assert "/health/ready" in paths
        assert client.get("/missing").status_code == 404


def test_readiness_reports_database_outage_without_leaking_password() -> None:
    settings = Settings(
        _env_file=None,
        database_password="must_never_appear_in_http",
        database_host="127.0.0.1",
        database_port=1,
        database_connect_timeout=1,
    )
    with TestClient(create_app(settings)) as client:
        response = client.get("/health/ready")
        asset_response = client.get(
            "/api/v1/files", headers={"Authorization": "Bearer test-session"}
        )
    assert response.status_code == 503
    assert response.json() == {"status": "not_ready", "code": "DATABASE_UNAVAILABLE"}
    assert "must_never_appear" not in response.text
    assert asset_response.status_code == 503
    assert asset_response.json()["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert "must_never_appear" not in asset_response.text
