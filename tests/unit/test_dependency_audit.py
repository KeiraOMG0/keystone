"""Weekly dependency audit (services/dependency_audit_service.py, cogs/dependency_audit.py).

Recorded-shape fixtures only: no pip-audit, npm or network in tests.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from cogs.dependency_audit import is_due
from services import dependency_audit_service as svc

PIP_JSON = json.dumps({
    "dependencies": [
        {"name": "brotli", "version": "1.2.0", "vulns": [
            {"id": "PYSEC-2026-4322", "fix_versions": ["1.2.1"], "aliases": [], "description": "x"}]},
        {"name": "aiohttp", "version": "3.13.5", "vulns": [
            {"id": "PYSEC-2026-2105", "fix_versions": ["3.14.0"]},
            {"id": "GHSA-2g87-pp6c-29x5", "fix_versions": ["3.14.4"]}]},
        {"name": "discord.py", "version": "2.7.1", "vulns": []},
    ],
    "fixes": [],
})

NPM_JSON = json.dumps({
    "auditReportVersion": 2,
    "vulnerabilities": {
        "react-router": {"name": "react-router", "severity": "high", "range": ">=7.0.0 <=7.11.0",
                         "via": [{"title": "XSS", "url": "https://github.com/advisories/GHSA-2w69-qvjg-hvjx"}],
                         "fixAvailable": True},
        "deepmerge": {"name": "deepmerge", "severity": "low", "range": "*", "via": [{"title": "proto"}],
                      "fixAvailable": {"name": "shadcn", "version": "1.0.0", "isSemVerMajor": True}},
        "shadcn": {"name": "shadcn", "severity": "low", "range": ">=2", "via": ["deepmerge"], "fixAvailable": False},
    },
})


def test_parse_pip_audit():
    found = svc.parse_pip_audit(PIP_JSON)
    by = {f.package: f for f in found}
    assert set(by) == {"brotli", "aiohttp"}  # clean packages are not findings
    assert by["aiohttp"].fix == "3.14.4"  # highest fix version
    assert by["aiohttp"].ids == ["GHSA-2g87-pp6c-29x5", "PYSEC-2026-2105"]
    assert by["brotli"].severity == "unknown" and by["brotli"].ecosystem == "python"


def test_parse_npm_audit():
    found = {f.package: f for f in svc.parse_npm_audit(NPM_JSON, "dashboard frontend")}
    assert found["react-router"].severity == "high" and found["react-router"].ids == ["GHSA-2w69-qvjg-hvjx"]
    assert found["react-router"].fix == "npm audit fix"
    assert found["deepmerge"].fix == "shadcn@1.0.0 (major)"
    assert found["shadcn"].fix is None and found["shadcn"].ids == []


def test_filters_drop_low_and_ignored():
    report = svc.AuditReport(findings=svc.parse_pip_audit(PIP_JSON) + svc.parse_npm_audit(NPM_JSON, "fe"))
    svc.apply_filters(report, "moderate", {"aiohttp"})
    assert sorted(f.package for f in report.findings) == ["brotli", "react-router"]
    assert report.ignored == 3  # deepmerge + shadcn (low) + aiohttp (ignored)


def test_outdated_ytdlp_note():
    assert svc.outdated_ytdlp(json.dumps([{"name": "yt-dlp", "version": "2026.6.9", "latest_version": "2026.10.26"}]))
    assert svc.outdated_ytdlp(json.dumps([{"name": "aiohttp", "version": "1", "latest_version": "2"}])) is None


def test_report_text_and_colour():
    report = svc.AuditReport(findings=svc.parse_npm_audit(NPM_JSON, "fe"))
    title, text, color = svc.build_report_text(report)
    assert "3 vulnerable" in title and color == svc.COLOR_RED  # worst is high
    assert "[high] `react-router`" in text and "npm audit fix" in text
    clean_but_failed = svc.AuditReport(errors=["pip-audit: FileNotFoundError"])
    title, text, color = svc.build_report_text(clean_but_failed)
    assert "could not run" in title and color == svc.COLOR_YELLOW
    huge = svc.AuditReport(findings=[svc.Finding("python", f"p{i}", "1", "unknown", ["X"] * 5, "2") for i in range(500)])
    assert len(svc.build_report_text(huge)[1]) <= 3800


def test_needs_action():
    assert svc.AuditReport().needs_action is False
    assert svc.AuditReport(notes=["yt-dlp old"]).needs_action is True


async def test_run_all_with_recorded_output(monkeypatch):
    calls = []

    async def fake_run(cmd, cwd, timeout):
        calls.append(cmd)
        if "pip_audit" in cmd:
            return 1, PIP_JSON, ""
        if "list" in cmd:
            return 0, json.dumps([{"name": "yt-dlp", "version": "1", "latest_version": "2"}]), ""
        return 1, NPM_JSON, ""

    monkeypatch.setattr(svc, "_run", fake_run)
    monkeypatch.setattr(svc.shutil, "which", lambda name: "npm")
    monkeypatch.delenv("DEPENDENCY_AUDIT_MIN_SEVERITY", raising=False)
    monkeypatch.delenv("DEPENDENCY_AUDIT_IGNORE", raising=False)
    report = await svc.run_all()
    assert report.checked == ["python", "dashboard frontend", "PO-token server"]
    # brotli + aiohttp (python) + react-router twice (two npm projects); lows ignored
    assert sorted(f.package for f in report.findings) == ["aiohttp", "brotli", "react-router", "react-router"]
    assert report.ignored == 4 and report.notes and report.needs_action


async def test_run_all_reports_missing_tools(monkeypatch):
    async def boom(cmd, cwd, timeout):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(svc, "_run", boom)
    monkeypatch.setattr(svc.shutil, "which", lambda name: None)
    report = await svc.run_all()
    assert "pip-audit: FileNotFoundError" in report.errors
    assert any("npm not found" in e for e in report.errors)
    assert report.needs_action  # silence must mean clean, so a failed check is reported


async def test_webhook_payload_and_no_pings(monkeypatch):
    sent = {}

    class FakeResp:
        status = 204

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    class FakeSession:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            sent["url"], sent["body"] = url, json
            return FakeResp()

    monkeypatch.setattr(svc.aiohttp, "ClientSession", FakeSession)
    report = svc.AuditReport(findings=svc.parse_pip_audit(PIP_JSON))
    assert await svc.send_webhook(report, url="https://discord.com/api/webhooks/1/x") is True
    body = sent["body"]
    assert body["allowed_mentions"] == {"parse": []} and body["content"] == ""
    assert body["embeds"][0]["title"].startswith("Dependency audit") and body["embeds"][0]["timestamp"]
    assert await svc.send_webhook(report, url="") is False  # no webhook configured


def test_webhook_url_fallback(monkeypatch):
    monkeypatch.delenv("DEPENDENCY_AUDIT_WEBHOOK_URL", raising=False)
    monkeypatch.setenv("SUPERVISOR_WEBHOOK_URL", "https://a")
    assert svc.webhook_url() == "https://a"
    monkeypatch.setenv("DEPENDENCY_AUDIT_WEBHOOK_URL", "https://b")
    assert svc.webhook_url() == "https://b"


TZ = dt.timezone(dt.timedelta(hours=-4))


@pytest.mark.parametrize("now,last,due", [
    (dt.datetime(2026, 11, 2, 4, 59, tzinfo=TZ), dt.datetime(2026, 10, 26, 9, 0, tzinfo=dt.timezone.utc), False),  # before Mon 05:00
    (dt.datetime(2026, 11, 2, 5, 0, tzinfo=TZ), dt.datetime(2026, 10, 26, 9, 0, tzinfo=dt.timezone.utc), True),  # the slot
    (dt.datetime(2026, 11, 2, 6, 0, tzinfo=TZ), dt.datetime(2026, 11, 2, 9, 30, tzinfo=dt.timezone.utc), False),  # already ran
    (dt.datetime(2026, 11, 5, 12, 0, tzinfo=TZ), dt.datetime(2026, 10, 20, 9, 0, tzinfo=dt.timezone.utc), True),  # catch-up
    (dt.datetime(2026, 11, 5, 12, 0, tzinfo=TZ), None, True),  # never ran
    (dt.datetime(2026, 11, 8, 23, 0, tzinfo=TZ), dt.datetime(2026, 11, 2, 9, 1, tzinfo=dt.timezone.utc), False),  # Sunday after
])
def test_is_due(now, last, due):
    assert is_due(last, now) is due
