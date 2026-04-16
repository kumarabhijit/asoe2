"""Server-side user profiles with role-based tab visibility and account scoping.

Users are real records (in-memory for V1, DB-backed in V1.1). Each user has:
  - A role from the existing RBAC system (analyst, manager, admin, viewer, partner)
  - Permissions derived from that role via _ROLE_PERMISSIONS (deps.py)
  - assigned_accounts for customer scope filtering (empty = all customers)
  - visible_tabs computed from permissions (not stored per user)

No parallel permission system — one role, one set of permissions, one truth.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel

from api.deps import _ROLE_PERMISSIONS


# ---------------------------------------------------------------------------
# User profile model (extends the JWT-level AuthenticatedUser)
# ---------------------------------------------------------------------------

class UserRecord(BaseModel):
    """A server-side user profile record."""

    sub: str
    email: str
    name: str
    title: str
    avatar_initials: str
    roles: List[str]
    org: str
    assigned_accounts: List[str] = []  # empty = all customers
    env: str = "sandbox"


# ---------------------------------------------------------------------------
# Tab visibility — derived from permissions, never stored per user
# ---------------------------------------------------------------------------

# Permission → tab mapping. A tab appears if the user has ANY of its permissions.
_PERMISSION_TAB_MAP: List[tuple[str, List[str]]] = [
    ("inbox",      ["exceptions:read"]),
    ("exceptions", ["exceptions:read"]),
    ("dashboard",  ["dashboard:read"]),
    ("settings",   ["rules:write", "policy:write", "users:manage"]),
]


def compute_visible_tabs(permissions: List[str]) -> List[str]:
    """Derive visible tabs from the user's expanded permissions."""
    perm_set = set(permissions)
    tabs: List[str] = []
    for tab_id, required_perms in _PERMISSION_TAB_MAP:
        if perm_set & set(required_perms):
            tabs.append(tab_id)
    return tabs


def expand_permissions(roles: List[str]) -> List[str]:
    """Expand role list to flat permission set (mirrors deps._expand_permissions)."""
    perms: set[str] = set()
    for role in roles:
        perms.update(_ROLE_PERMISSIONS.get(role, []))
    return sorted(perms)


# ---------------------------------------------------------------------------
# Seed users — 5 personas matching prototype, each with a real RBAC role
# ---------------------------------------------------------------------------

_SEED_USERS: List[UserRecord] = [
    UserRecord(
        sub="usr_marcus_webb",
        email="marcus.webb@acme-corp.com",
        name="Marcus Webb",
        title="Admin",
        avatar_initials="MW",
        roles=["admin"],
        org="acme-corp",
        assigned_accounts=[],  # all customers
    ),
    UserRecord(
        sub="usr_sarah_chen_mgr",
        email="sarah.chen@acme-corp.com",
        name="Sarah Chen",
        title="CS Manager",
        avatar_initials="SC",
        roles=["manager"],
        org="acme-corp",
        assigned_accounts=[],  # all customers
    ),
    UserRecord(
        sub="usr_sarah_chen_sr",
        email="sarah.chen.sr@acme-corp.com",
        name="Sarah Chen",
        title="Sr. CS Analyst",
        avatar_initials="SC",
        roles=["analyst"],
        org="acme-corp",
        assigned_accounts=[],  # all customers
    ),
    UserRecord(
        sub="usr_james_ortiz",
        email="james.ortiz@acme-corp.com",
        name="James Ortiz",
        title="CS Analyst",
        avatar_initials="JO",
        roles=["analyst"],
        org="acme-corp",
        assigned_accounts=["Walmart", "Kroger"],
    ),
    UserRecord(
        sub="usr_priya_nair",
        email="priya.nair@acme-corp.com",
        name="Priya Nair",
        title="Trade Analyst",
        avatar_initials="PN",
        roles=["analyst"],
        org="acme-corp",
        assigned_accounts=["Target", "Costco"],
    ),
]

# Index by email for login lookup, by sub for token lookup
_USERS_BY_EMAIL: Dict[str, UserRecord] = {u.email: u for u in _SEED_USERS}
_USERS_BY_SUB: Dict[str, UserRecord] = {u.sub: u for u in _SEED_USERS}


def get_user_by_email(email: str) -> Optional[UserRecord]:
    """Look up a user by email (case-insensitive)."""
    return _USERS_BY_EMAIL.get(email.lower()) or _USERS_BY_EMAIL.get(email)


def get_user_by_sub(sub: str) -> Optional[UserRecord]:
    """Look up a user by subject ID."""
    return _USERS_BY_SUB.get(sub)


def list_users() -> List[UserRecord]:
    """Return all seed users (for sandbox user switcher)."""
    return list(_SEED_USERS)
