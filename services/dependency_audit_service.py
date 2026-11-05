"""
Weekly dependency audit (2026-10-30): pip-audit on the bot's venv and
`npm audit` on the dashboard frontend and the PO-token server, reported to a
Discord webhook when something needs updating.

Used by cogs/dependency_audit.py (weekly schedule + `!depaudit`). Pure parsing
helpers (`parse_pip_audit`, `parse_npm_audit`, `build_report`) are unit-tested
with recorded output; `run_all()` shells out.

* Python: `<venv python> -m pip_audit --format json` audits the INSTALLED
  environment (what actually runs), so anything installed by hand is covered.
  pip-audit exits non-zero when it finds something; the JSON is read anyway.
* Node: `npm audit --omit=dev --json` in each project (dev-only tooling never
  runs in production). The JSON is read whatever the exit code.
* yt-dlp is also checked for being out of date (`pip list --outdated`), since an
  old yt-dlp is what usually breaks music, vulnerability or not.

Noise control: npm findings below DEPENDENCY_AUDIT_MIN_SEVERITY (default
"moderate"; low/info are counted as `ignored`) and any package listed in
DEPENDENCY_AUDIT_IGNORE (comma separated) are not reported. pip-audit gives no
severity, so every Python finding is reported unless its package is ignored.

Webhook: DEPENDENCY_AUDIT_WEBHOOK_URL, else SUPERVISOR_WEBHOOK_URL (the crash-
alert webhook), same embed format as supervisor.py's send_webhook. A report is
posted only when something needs action (a vulnerability, an outdated yt-dlp,
or a check that could not run); a clean week posts nothing. Never raises.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
NPM_PROJECTS: tuple[tuple[str, Path], ...] = (
    ("dashboard frontend", ROOT / "server" / "frontend"),
    ("PO-token server", ROOT / "pot_server" / "server"),
)
PIP_TIMEOUT = 300
NPM_TIMEOUT = 180
AUDIT_ACTION = "dependency_audit_run"

SEVERITY_ORDER = ("critical", "high", "moderate", "low", "info", "unknown")
COLOR_RED, COLOR_ORANGE, COLOR_YELLOW = 0xED4245, 0xF0B232, 0xFEE75C


@dataclass
class Finding:
    ecosystem: str  # "python" | project label
    package: str
    version: str | None
    severity: str
    ids: list[str]
    fix: str | None  # version / "available" / None


@dataclass
class AuditReport:
    findings: list[Finding] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # e.g. outdated yt-dlp
    checked: list[str] = field(default_factory=list)
    ignored: int = 0

    @property
    def needs_action(self) -> bool:
        return bool(self.findings or self.errors or self.notes)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.severity] = out.get(f.severity, 0) + 1
        return out

    def summary(self) -> dict:
        return {"vulnerable_packages": len(self.findings), "by_severity": self.counts(),
                "ignored": self.ignored, "errors": self.errors, "notes": self.notes, "checked": self.checked}


# ---------------------------------------------------------------------------
# Parsing (pure)
# ---------------------------------------------------------------------------

def parse_pip_audit(raw: str) -> list[Finding]:
    """pip-audit --format json -> one Finding per vulnerable package."""
    data = json.loads(raw)
    deps = data.get("dependencies", data) if isinstance(data, dict) else data
    out = []
    for dep in deps or []:
        vulns = dep.get("vulns") or []
        if not vulns:
            continue
        fixes = sorted({v for vuln in vulns for v in (vuln.get("fix_versions") or [])}, key=_version_key)
        out.append(Finding(
            ecosystem="python", package=dep.get("name", "?"), version=dep.get("version"),
            severity="unknown",  # pip-audit's JSON carries no severity
            ids=sorted({vuln.get("id", "?") for vuln in vulns}),
            fix=fixes[-1] if fixes else None,
        ))
    return out


def parse_npm_audit(raw: str, label: str) -> list[Finding]:
    """npm audit --json (npm 7+) -> one Finding per vulnerable package."""
    data = json.loads(raw)
    out = []
    for name, vuln in (data.get("vulnerabilities") or {}).items():
        ids = []
        for via in vuln.get("via") or []:
            if isinstance(via, dict):
                url = str(via.get("url") or "")
                ids.append(url.rsplit("/", 1)[-1] if url else str(via.get("title") or via.get("source") or "?"))
        fix = vuln.get("fixAvailable")
        if isinstance(fix, dict):
            fix_text = f"{fix.get('name')}@{fix.get('version')}" + (" (major)" if fix.get("isSemVerMajor") else "")
        else:
            fix_text = "npm audit fix" if fix else None
        out.append(Finding(
            ecosystem=label, package=name, version=str(vuln.get("range") or "") or None,
            severity=str(vuln.get("severity") or "unknown"), ids=sorted(set(ids)), fix=fix_text,
        ))
    return out


def outdated_ytdlp(raw: str) -> str | None:
    """pip list --outdated --format json -> a note when yt-dlp is behind."""
    for pkg in json.loads(raw or "[]"):
        if str(pkg.get("name", "")).lower() == "yt-dlp":
            return (f"yt-dlp {pkg.get('version')} -> {pkg.get('latest_version')} is available "
                    "(music breaks when yt-dlp falls behind YouTube)")
    return None


def min_severity() -> str:
    value = os.environ.get("DEPENDENCY_AUDIT_MIN_SEVERITY", "moderate").strip().lower()
    return value if value in SEVERITY_ORDER else "moderate"


def ignored_packages() -> set[str]:
    return {p.strip().lower() for p in os.environ.get("DEPENDENCY_AUDIT_IGNORE", "").split(",") if p.strip()}


def apply_filters(report: "AuditReport", minimum: str, ignore: set[str]) -> None:
    """Drop findings below `minimum` (npm only; Python has no severity) or in
    `ignore`; count them in report.ignored."""
    floor = SEVERITY_ORDER.index(minimum)
    kept = []
    for f in report.findings:
        rank = SEVERITY_ORDER.index(f.severity) if f.severity in SEVERITY_ORDER else len(SEVERITY_ORDER)
        too_low = f.severity != "unknown" and rank > floor
        if too_low or f.package.lower() in ignore:
            report.ignored += 1
        else:
            kept.append(f)
    report.findings = kept


def _version_key(v: str):
    parts = []
    for p in str(v).replace("-", ".").split("."):
        parts.append((0, int(p)) if p.isdigit() else (1, p))
    return parts


def build_report_text(report: AuditReport, limit: int = 3800) -> tuple[str, str, int]:
    """(title, description, colour) for the webhook embed."""
    counts = report.counts()
    worst = next((s for s in SEVERITY_ORDER if counts.get(s)), None)
    if report.findings:
        title = f"Dependency audit: {len(report.findings)} vulnerable package(s)"
    elif report.notes:
        title = "Dependency audit: updates recommended"
    else:
        title = "Dependency audit: a check could not run"
    color = COLOR_RED if worst in ("critical", "high") else COLOR_ORANGE if report.findings else COLOR_YELLOW
    lines: list[str] = []
    by_eco: dict[str, list[Finding]] = {}
    for f in report.findings:
        by_eco.setdefault(f.ecosystem, []).append(f)
    for eco, items in by_eco.items():
        lines.append(f"**{'Python (venv)' if eco == 'python' else eco}**")
        items.sort(key=lambda f: (SEVERITY_ORDER.index(f.severity) if f.severity in SEVERITY_ORDER else 9, f.package))
        for f in items:
            sev = "" if f.severity == "unknown" else f"[{f.severity}] "
            ver = f" {f.version}" if f.version else ""
            fix = f" -> fix: {f.fix}" if f.fix else " (no fix yet)"
            ids = ", ".join(f.ids[:3]) + (f" +{len(f.ids) - 3}" if len(f.ids) > 3 else "")
            lines.append(f"- {sev}`{f.package}`{ver}{fix} ({ids})")
    for note in report.notes:
        lines.append(f"- {note}")
    for err in report.errors:
        lines.append(f"- Check failed: {err}")
    lines.append("")
    lines.append("Fix: Diagnostics > Services > Dependency updates > Update now (owner only; "
                 "or `supervisor.py update-deps apply`): applies the safe fixes, rebuilds, tests, "
                 "rolls back on failure and restarts what needs it. Major upgrades stay manual. "
                 "Re-check with `!depaudit`.")
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[: limit - 20] + "\n... (truncated)"
    return title, text, color


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------

async def _run(cmd: list[str], cwd: Path, timeout: int) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")


async def run_all() -> AuditReport:
    report = AuditReport()
    python = sys.executable  # the bot's venv interpreter
    try:
        _, out, err = await _run([python, "-m", "pip_audit", "--format", "json", "--progress-spinner", "off"],
                                 ROOT, PIP_TIMEOUT)
        report.findings += parse_pip_audit(out)
        report.checked.append("python")
    except Exception as exc:  # not installed, offline, timeout, bad JSON
        report.errors.append(f"pip-audit: {type(exc).__name__}")
        log.warning("dependency audit: pip-audit failed", exc_info=True)
    try:
        _, out, _ = await _run([python, "-m", "pip", "list", "--outdated", "--format", "json",
                                "--disable-pip-version-check"], ROOT, PIP_TIMEOUT)
        note = outdated_ytdlp(out)
        if note:
            report.notes.append(note)
    except Exception:
        log.warning("dependency audit: pip list --outdated failed", exc_info=True)
    npm = shutil.which("npm")
    for label, path in NPM_PROJECTS:
        if not (path / "package-lock.json").is_file():
            continue
        if not npm:
            report.errors.append(f"{label}: npm not found on PATH")
            continue
        try:
            _, out, _ = await _run([npm, "audit", "--omit=dev", "--json"], path, NPM_TIMEOUT)
            report.findings += parse_npm_audit(out, label)
            report.checked.append(label)
        except Exception as exc:
            report.errors.append(f"npm audit ({label}): {type(exc).__name__}")
            log.warning("dependency audit: npm audit failed for %s", label, exc_info=True)
    apply_filters(report, min_severity(), ignored_packages())
    return report


def webhook_url() -> str:
    return (os.environ.get("DEPENDENCY_AUDIT_WEBHOOK_URL", "").strip()
            or os.environ.get("SUPERVISOR_WEBHOOK_URL", "").strip())


async def send_webhook(report: AuditReport, url: str | None = None) -> bool:
    """Post the report (same embed shape as supervisor.py). False when there is
    no webhook or Discord refused it. Never pings anyone."""
    url = url if url is not None else webhook_url()
    if not url:
        return False
    title, description, color = build_report_text(report)
    body: dict[str, Any] = {
        "content": "",
        "allowed_mentions": {"parse": []},
        "embeds": [{"title": title, "description": description, "color": color,
                    "timestamp": datetime.now(timezone.utc).isoformat()}],
    }
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            async with session.post(url, json=body, headers={"User-Agent": "kick-bot-dependency-audit"}) as resp:
                return resp.status < 300
    except Exception:
        log.warning("dependency audit: webhook post failed", exc_info=False)
        return False
