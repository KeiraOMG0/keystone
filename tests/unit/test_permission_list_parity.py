"""The bot permission strings are listed in several places that must agree:
services/permission_service.py VALID_PERMISSIONS (the source of truth),
the /bot permissions grant|revoke choices in cogs/advanced_commands.py,
_ALL_PERMISSIONS in server/routes/me.py, and the column labels in the
frontend Permissions page (parsed as text). Fails on any drift."""

from __future__ import annotations

import ast
import pathlib
import re

from services.permission_service import VALID_PERMISSIONS

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _module_list(path: pathlib.Path, name: str) -> list[str]:
    """Read a module-level list literal without importing the module."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            return list(ast.literal_eval(node.value))
    raise AssertionError(f"{name} not found in {path}")


def test_valid_permissions_shape():
    assert all(re.fullmatch(r"[a-z_]+", p) for p in VALID_PERMISSIONS)


def test_dashboard_me_list_matches():
    perms = _module_list(ROOT / "server" / "routes" / "me.py", "_ALL_PERMISSIONS")
    assert len(perms) == len(set(perms)), "duplicate in _ALL_PERMISSIONS"
    assert set(perms) == VALID_PERMISSIONS


def test_slash_command_choices_match():
    from cogs.advanced_commands import PERMISSION_CHOICES

    values = [c.value for c in PERMISSION_CHOICES]
    assert set(values) == VALID_PERMISSIONS and len(values) == len(set(values))
    assert len(values) <= 25  # Discord's choice limit
    src = (ROOT / "cogs" / "advanced_commands.py").read_text(encoding="utf-8")
    assert src.count("@app_commands.choices(permission=PERMISSION_CHOICES)") == 2


def test_frontend_permission_labels_only_name_real_permissions():
    path = ROOT / "server" / "frontend" / "src" / "pages" / "permissions" / "PermissionsPage.tsx"
    src = path.read_text(encoding="utf-8")
    m = re.search(r"PERMISSION_LABELS[^=]*=\s*\{(.*?)\n\}", src, re.S)
    assert m, "PERMISSION_LABELS not found in PermissionsPage.tsx"
    keys = set(re.findall(r"^\s*([a-z_]+)\s*:", m.group(1), re.M))
    assert keys, "no keys parsed from PERMISSION_LABELS"
    stale = keys - VALID_PERMISSIONS
    assert not stale, f"PermissionsPage.tsx labels for unknown permissions: {sorted(stale)}"
