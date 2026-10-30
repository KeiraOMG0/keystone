"""Public Privacy Policy page (/privacy).

* The dashboard backend's SPA fallback serves index.html for /privacy on a
  hard refresh, with no session (it is the Privacy Policy URL registered in
  the Discord Developer Portal, so it must never need a login).
* The route is registered as a public route (outside RequireAuth/AppShell).
* The content module never contains an email address (none exists for this
  project; CONTACT_EXTRA is the placeholder) and keeps the retention numbers
  in step with the code they describe.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server.app as server_app

ROOT = Path(__file__).resolve().parents[2]
FRONTEND_SRC = ROOT / "server" / "frontend" / "src"
CONTENT = FRONTEND_SRC / "pages" / "privacy" / "privacyContent.tsx"
ROUTES = FRONTEND_SRC / "app" / "routes.tsx"
INDEX = "<!doctype html><title>spa-index</title><div id=root></div>"


@pytest.fixture
def client(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX, encoding="utf-8")
    monkeypatch.setattr(server_app, "_FRONTEND_DIST", dist)
    return TestClient(server_app.create_app())


@pytest.mark.parametrize("path", ["/privacy", "/privacy#cookies"])
def test_privacy_serves_index_without_session(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "spa-index" in r.text


def test_api_privacy_is_not_the_spa(client):
    r = client.get("/api/privacy")
    assert r.status_code == 404
    assert r.json() == {"error": "not_found"}


def test_privacy_route_is_public():
    src = ROUTES.read_text(encoding="utf-8")
    # A top-level route, declared before the protected /dashboard layout route.
    privacy = src.index('path="/privacy"')
    dashboard = src.index('path="/dashboard"')
    assert privacy < dashboard


def test_content_has_no_email_address():
    text = CONTENT.read_text(encoding="utf-8")
    assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text), "never put an email address in the privacy policy"
    assert "export const CONTACT_EXTRA: string | null = null" in text


def test_retention_numbers_match_code():
    import config
    from services import audit_service

    text = CONTENT.read_text(encoding="utf-8")
    assert audit_service.AUDIT_RETENTION_DAYS == 365 and "365 days" in text
    # Defaults (env may override on a given machine; the policy states the defaults).
    assert "MESSAGE_LOG_RETENTION_DAYS\", 7" in (ROOT / "config.py").read_text(encoding="utf-8")
    assert "7 days" in text
    assert 'TICKET_ARCHIVE_PURGE_DAYS") or "30"' in (ROOT / "config.py").read_text(encoding="utf-8")
    assert "30 days after it is closed" in text
    from server.services import error_reports

    assert error_reports.ERROR_REPORT_RETENTION_DAYS == 30 and "error reports" in text.lower()
    assert hasattr(config, "MESSAGE_LOG_RETENTION_DAYS")
    from server import config as server_config

    assert server_config.SESSION_COOKIE_NAME in text
    assert server_config.OAUTH_STATE_COOKIE_NAME in text
    from server.routes import appeal_auth

    assert appeal_auth.IDENTITY_COOKIE in text and appeal_auth.STATE_COOKIE in text
