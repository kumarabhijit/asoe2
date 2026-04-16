"""Server-side user profiles and accounts.

Users are real records (in-memory for V1, DB-backed in V1.1). Each user has:
  - A role from the existing RBAC system (analyst, manager, admin, viewer, partner)
  - Permissions derived from that role via _ROLE_PERMISSIONS (deps.py)
  - assigned_accounts for customer scope filtering (empty = all customers)
  - visible_tabs computed from permissions (not stored per user)

Accounts are the first-class "customer" entity within a tenant.
  - Tenants = manufacturers (e.g., acme-corp)
  - Accounts = retail customers (e.g., Walmart, Kroger) within a tenant
  - Each exception is associated with an account_id

No parallel permission system — one role, one set of permissions, one truth.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel

from api.deps import _ROLE_PERMISSIONS


# ---------------------------------------------------------------------------
# Account model — first-class customer entity within a tenant
# ---------------------------------------------------------------------------

class Account(BaseModel):
    """A retail customer account within a tenant (manufacturer)."""

    id: str
    name: str
    tenant_id: str
    bp_number: str  # SAP Business Partner number
    tier: str  # "enterprise" | "strategic" | "standard"
    region: Optional[str] = None


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
    assigned_accounts: List[str] = []  # account IDs — empty = all
    env: str = "sandbox"


# ---------------------------------------------------------------------------
# Tab visibility — derived from permissions, never stored per user
# ---------------------------------------------------------------------------

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
# Seed accounts — retail customers within acme-corp tenant
# ---------------------------------------------------------------------------

_SEED_ACCOUNTS: List[Account] = [
    Account(id="acct-walmart", name="Walmart", tenant_id="acme-corp", bp_number="BP-0001", tier="enterprise", region="National"),
    Account(id="acct-kroger", name="Kroger", tenant_id="acme-corp", bp_number="BP-0002", tier="enterprise", region="Midwest"),
    Account(id="acct-target", name="Target", tenant_id="acme-corp", bp_number="BP-0003", tier="strategic", region="National"),
    Account(id="acct-costco", name="Costco", tenant_id="acme-corp", bp_number="BP-0004", tier="strategic", region="West"),
]

_ACCOUNTS_BY_ID: Dict[str, Account] = {a.id: a for a in _SEED_ACCOUNTS}
_ACCOUNTS_BY_NAME: Dict[str, Account] = {a.name: a for a in _SEED_ACCOUNTS}


def get_account(account_id: str) -> Optional[Account]:
    """Look up an account by ID."""
    return _ACCOUNTS_BY_ID.get(account_id)


def get_account_by_name(name: str) -> Optional[Account]:
    """Look up an account by name."""
    return _ACCOUNTS_BY_NAME.get(name)


def list_accounts(tenant_id: str) -> List[Account]:
    """Return all accounts for a tenant."""
    return [a for a in _SEED_ACCOUNTS if a.tenant_id == tenant_id]


# ---------------------------------------------------------------------------
# Seed users — 5 personas matching prototype, each with a real RBAC role
# ---------------------------------------------------------------------------

_SEED_USERS: List[UserRecord] = [
    UserRecord(
        sub="usr_jane_doe",
        email="jane@acme.com",
        name="Jane Doe",
        title="Admin",
        avatar_initials="JD",
        roles=["admin"],
        org="acme-corp",
        assigned_accounts=[],  # all accounts
    ),
    UserRecord(
        sub="usr_marcus_webb",
        email="marcus.webb@acme-corp.com",
        name="Marcus Webb",
        title="Admin",
        avatar_initials="MW",
        roles=["admin"],
        org="acme-corp",
        assigned_accounts=[],  # all accounts
    ),
    UserRecord(
        sub="usr_sarah_chen_mgr",
        email="sarah.chen@acme-corp.com",
        name="Sarah Chen",
        title="CS Manager",
        avatar_initials="SC",
        roles=["manager"],
        org="acme-corp",
        assigned_accounts=[],  # all accounts
    ),
    UserRecord(
        sub="usr_sarah_chen_sr",
        email="sarah.chen.sr@acme-corp.com",
        name="Sarah Chen",
        title="Sr. CS Analyst",
        avatar_initials="SC",
        roles=["analyst"],
        org="acme-corp",
        assigned_accounts=[],  # all accounts
    ),
    UserRecord(
        sub="usr_james_ortiz",
        email="james.ortiz@acme-corp.com",
        name="James Ortiz",
        title="CS Analyst",
        avatar_initials="JO",
        roles=["analyst"],
        org="acme-corp",
        assigned_accounts=["acct-walmart", "acct-kroger"],
    ),
    UserRecord(
        sub="usr_priya_nair",
        email="priya.nair@acme-corp.com",
        name="Priya Nair",
        title="Trade Analyst",
        avatar_initials="PN",
        roles=["analyst"],
        org="acme-corp",
        assigned_accounts=["acct-target", "acct-costco"],
    ),
]

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
