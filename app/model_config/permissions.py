"""M0 permission policy. HTTP authorization must also verify live account/grants."""
from __future__ import annotations

import json
from pathlib import Path
import re

_path = Path(__file__).resolve().parents[2] / "config/model_config_permissions.json"
_policy = json.loads(_path.read_text(encoding="utf-8"))
PERMISSIONS = frozenset(_policy["roles"]["config_admin"])
ROLE_PERMISSIONS = {role: frozenset(permissions) for role, permissions in _policy["roles"].items()}
ROUTES = tuple(_policy["routes"])
if any(not values.issubset(PERMISSIONS) for values in ROLE_PERMISSIONS.values()):
    raise ValueError("Unknown model configuration permission")
if any(row["enabled"] and row["permission"] not in PERMISSIONS for row in ROUTES):
    raise ValueError("Unmapped model configuration route")


def allows_config_action(role: str | None, permission: str | None) -> bool:
    return permission in ROLE_PERMISSIONS.get(role, frozenset())


def required_config_permission(method: str, path: str) -> str | None:
    """Unknown/disabled routes have no permission mapping and must be rejected."""
    for route in ROUTES:
        pattern = re.sub(r"\\\{[^}]+\\\}", r"[^/]+", re.escape(route["path"]))
        if route["enabled"] and method.upper() == route["method"] and re.fullmatch(pattern, path.rstrip("/")):
            return route["permission"]
    return None
