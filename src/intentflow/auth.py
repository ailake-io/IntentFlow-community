"""Roles and permissions. No registered users = local mode (a single operator, loopback only).
Per-project roles and write scopes are configured through IntentFlow Enterprise, but stored grants are always honored:
losing the license must never widen access (a `none` on a project keeps blocking it)."""
from __future__ import annotations

RANK = {"none": 0, "viewer": 1, "engineer": 2, "admin": 3}

# action → minimum role
NEED = {
    "read": "viewer",
    "ack": "engineer", "false_positive": "engineer", "assign": "engineer", "note": "engineer",
    "fix_create": "engineer", "fix_validate": "engineer", "fix_approve": "engineer", "fix_reject": "engineer",
    "fix_apply": "engineer", "fix_rollback": "engineer",
    "run_check": "engineer", "run_collect": "engineer", "diff": "engineer",
    "settings": "admin", "users": "admin",
}


def allowed(role: str | None, action: str) -> bool:
    return RANK.get(role or "", 0) >= RANK[NEED[action]]


def role_in(user: dict | None, project: str | None) -> str | None:
    """Effective role of the person in the project: the project-specific role, if any, otherwise the default role."""
    if not user:
        return None
    pr = user.get("project_roles") or {}
    if project and project in pr:
        return pr[project]
    return user.get("role")
