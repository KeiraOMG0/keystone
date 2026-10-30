"""Public SPA pages (/terms, /hub, plus the existing /status and /appeal) are
served index.html by the dashboard backend's SPA fallback on a hard refresh,
with no session, while unknown /api/* paths still get the JSON 404."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import server.app as server_app

INDEX = "<!doctype html><title>spa-index</title><div id=root></div>"


@pytest.fixture
def client(tmp_path, monkeypatch):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(INDEX, encoding="utf-8")
    monkeypatch.setattr(server_app, "_FRONTEND_DIST", dist)
    return TestClient(server_app.create_app())


@pytest.mark.parametrize("path", ["/terms", "/hub", "/status", "/appeal", "/terms#contact"])
def test_public_pages_serve_index_without_session(client, path):
    r = client.get(path)
    assert r.status_code == 200
    assert "spa-index" in r.text


def test_unknown_api_path_is_still_json_404(client):
    r = client.get("/api/terms")
    assert r.status_code == 404
    assert r.json() == {"error": "not_found"}
