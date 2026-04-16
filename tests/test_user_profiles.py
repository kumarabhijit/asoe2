"""Tests for server-side user profiles, accounts, and RBAC-derived features.

Covers:
- api/users.py: compute_visible_tabs, expand_permissions, user/account lookups
- api/routes/accounts.py: GET /api/v1/accounts with scoping
- api/routes/auth.py: POST /api/auth/switch edge cases, GET /api/auth/users production mode
- api/routes/exceptions.py: account_id scoping on exception list
- api/deps.py: JWT claims round-trip (title, avatar_initials, assigned_accounts)
- api/store.py: account_id on ExceptionRecord round-trip
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from api.deps import (
    _get_jwt_secret,
    _jwt_decode,
    create_access_token,
    create_test_token,
)
from api.store import exception_store
from api.users import (
    Account,
    compute_visible_tabs,
    expand_permissions,
    get_account,
    get_account_by_name,
    get_user_by_email,
    get_user_by_sub,
    list_accounts,
    list_users,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def app():
    return create_app()


@pytest.fixture()
def client(app):
    exception_store.clear()
    return TestClient(app, raise_server_exceptions=False)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _login(client, email="marcus.webb@acme-corp.com"):
    """Helper: login and return access token."""
    r = client.post("/api/auth/login", json={"email": email, "password": "password"})
    assert r.status_code == 200
    return r.json()["access_token"]


def _create_exception_with_account(client, token, order_id="PO-TEST", account_id=None, account_name=None):
    """Create an exception via resolve, then manually set account_id."""
    r = client.post(
        "/api/v1/exceptions/resolve",
        json={"order_id": order_id, "po_price": 100.0, "sap_base_price": 120.0, "event_type": "EDI_850_PRICE_MISMATCH"},
        headers=_auth(token),
    )
    exc_id = r.json()["exception_id"]
    # Manually set account_id on the in-memory record
    record = exception_store._records.get(exc_id)
    if record and account_id:
        record.account_id = account_id
        record.account_name = account_name
    return exc_id


# ===========================================================================
# Unit Tests: api/users.py
# ===========================================================================


class TestComputeVisibleTabs:
    """compute_visible_tabs() derives tabs from permissions."""

    def test_admin_sees_all_tabs(self):
        perms = expand_permissions(["admin"])
        tabs = compute_visible_tabs(perms)
        assert "inbox" in tabs
        assert "exceptions" in tabs
        assert "dashboard" in tabs
        assert "settings" in tabs

    def test_manager_sees_settings(self):
        perms = expand_permissions(["manager"])
        tabs = compute_visible_tabs(perms)
        assert "settings" in tabs  # has rules:write

    def test_analyst_no_settings(self):
        perms = expand_permissions(["analyst"])
        tabs = compute_visible_tabs(perms)
        assert "settings" not in tabs
        assert "inbox" in tabs
        assert "exceptions" in tabs
        assert "dashboard" in tabs

    def test_viewer_no_settings(self):
        perms = expand_permissions(["viewer"])
        tabs = compute_visible_tabs(perms)
        assert "settings" not in tabs
        assert "dashboard" in tabs
        assert "inbox" in tabs

    def test_partner_minimal(self):
        perms = expand_permissions(["partner"])
        tabs = compute_visible_tabs(perms)
        assert "inbox" in tabs
        assert "exceptions" in tabs
        assert "dashboard" not in tabs
        assert "settings" not in tabs

    def test_empty_permissions(self):
        tabs = compute_visible_tabs([])
        assert tabs == []

    def test_unknown_role(self):
        perms = expand_permissions(["nonexistent"])
        tabs = compute_visible_tabs(perms)
        assert tabs == []


class TestExpandPermissions:
    """expand_permissions() expands roles to permission sets."""

    def test_analyst_permissions(self):
        perms = expand_permissions(["analyst"])
        assert "exceptions:read" in perms
        assert "exceptions:approve" in perms
        assert "dashboard:read" in perms
        assert "exceptions:override" not in perms

    def test_admin_has_all(self):
        perms = expand_permissions(["admin"])
        assert "users:manage" in perms
        assert "policy:write" in perms
        assert "audit:read" in perms

    def test_multiple_roles_union(self):
        perms = expand_permissions(["analyst", "viewer"])
        # Should be union of both (analyst superset of viewer)
        assert "exceptions:approve" in perms
        assert "dashboard:read" in perms

    def test_empty_roles(self):
        assert expand_permissions([]) == []


class TestUserLookups:
    """get_user_by_email, get_user_by_sub, list_users."""

    def test_get_user_by_email_found(self):
        user = get_user_by_email("marcus.webb@acme-corp.com")
        assert user is not None
        assert user.name == "Marcus Webb"
        assert user.roles == ["admin"]

    def test_get_user_by_email_not_found(self):
        assert get_user_by_email("nobody@example.com") is None

    def test_get_user_by_sub_found(self):
        user = get_user_by_sub("usr_james_ortiz")
        assert user is not None
        assert user.name == "James Ortiz"
        assert user.assigned_accounts == ["acct-walmart", "acct-kroger"]

    def test_get_user_by_sub_not_found(self):
        assert get_user_by_sub("usr_nonexistent") is None

    def test_list_users_returns_five(self):
        users = list_users()
        assert len(users) == 5
        names = [u.name for u in users]
        assert "Marcus Webb" in names
        assert "James Ortiz" in names
        assert "Priya Nair" in names


class TestAccountLookups:
    """Account entity lookups."""

    def test_get_account_found(self):
        acct = get_account("acct-walmart")
        assert acct is not None
        assert acct.name == "Walmart"
        assert acct.tier == "enterprise"

    def test_get_account_not_found(self):
        assert get_account("acct-nonexistent") is None

    def test_get_account_by_name(self):
        acct = get_account_by_name("Kroger")
        assert acct is not None
        assert acct.id == "acct-kroger"

    def test_list_accounts_by_tenant(self):
        accounts = list_accounts("acme-corp")
        assert len(accounts) == 4
        ids = [a.id for a in accounts]
        assert "acct-walmart" in ids
        assert "acct-costco" in ids

    def test_list_accounts_unknown_tenant(self):
        accounts = list_accounts("unknown-tenant")
        assert accounts == []

    def test_account_model_fields(self):
        acct = Account(id="test", name="Test Co", tenant_id="t1", bp_number="BP-999", tier="standard")
        assert acct.region is None  # optional field default


# ===========================================================================
# Integration Tests: GET /api/v1/accounts
# ===========================================================================


class TestAccountsEndpoint:
    """GET /api/v1/accounts — scoped by assigned_accounts."""

    def test_admin_sees_all_accounts(self, client):
        token = _login(client, "marcus.webb@acme-corp.com")
        r = client.get("/api/v1/accounts", headers=_auth(token))
        assert r.status_code == 200
        data = r.json()["data"]
        assert len(data) == 4
        names = [a["name"] for a in data]
        assert "Walmart" in names
        assert "Costco" in names

    def test_scoped_analyst_sees_only_assigned(self, client):
        token = _login(client, "james.ortiz@acme-corp.com")
        r = client.get("/api/v1/accounts", headers=_auth(token))
        assert r.status_code == 200
        data = r.json()["data"]
        assert len(data) == 2
        names = [a["name"] for a in data]
        assert "Walmart" in names
        assert "Kroger" in names
        assert "Target" not in names

    def test_different_scoped_analyst(self, client):
        token = _login(client, "priya.nair@acme-corp.com")
        r = client.get("/api/v1/accounts", headers=_auth(token))
        assert r.status_code == 200
        data = r.json()["data"]
        assert len(data) == 2
        names = [a["name"] for a in data]
        assert "Target" in names
        assert "Costco" in names

    def test_account_fields(self, client):
        token = _login(client, "marcus.webb@acme-corp.com")
        r = client.get("/api/v1/accounts", headers=_auth(token))
        acct = r.json()["data"][0]
        assert "id" in acct
        assert "name" in acct
        assert "bp_number" in acct
        assert "tier" in acct

    def test_unauthenticated(self, client):
        r = client.get("/api/v1/accounts")
        assert r.status_code == 401


# ===========================================================================
# Integration Tests: Account scoping on exception list
# ===========================================================================


class TestExceptionAccountScoping:
    """GET /api/v1/exceptions — assigned_accounts filters by account_id."""

    def test_admin_sees_all_exceptions(self, client):
        admin_token = _login(client, "marcus.webb@acme-corp.com")
        _create_exception_with_account(client, admin_token, "PO-W1", "acct-walmart", "Walmart")
        _create_exception_with_account(client, admin_token, "PO-T1", "acct-target", "Target")
        _create_exception_with_account(client, admin_token, "PO-K1", "acct-kroger", "Kroger")

        r = client.get("/api/v1/exceptions", headers=_auth(admin_token))
        assert r.status_code == 200
        assert len(r.json()["data"]) == 3

    def test_scoped_user_sees_only_assigned_accounts(self, client):
        admin_token = _login(client, "marcus.webb@acme-corp.com")
        _create_exception_with_account(client, admin_token, "PO-W2", "acct-walmart", "Walmart")
        _create_exception_with_account(client, admin_token, "PO-T2", "acct-target", "Target")
        _create_exception_with_account(client, admin_token, "PO-K2", "acct-kroger", "Kroger")
        _create_exception_with_account(client, admin_token, "PO-C2", "acct-costco", "Costco")

        # James Ortiz sees only Walmart + Kroger
        james_token = _login(client, "james.ortiz@acme-corp.com")
        r = client.get("/api/v1/exceptions", headers=_auth(james_token))
        assert r.status_code == 200
        data = r.json()["data"]
        assert len(data) == 2
        account_ids = [e["account_id"] for e in data]
        assert all(a in ("acct-walmart", "acct-kroger") for a in account_ids)

    def test_different_scoped_user_sees_different_accounts(self, client):
        admin_token = _login(client, "marcus.webb@acme-corp.com")
        _create_exception_with_account(client, admin_token, "PO-W3", "acct-walmart", "Walmart")
        _create_exception_with_account(client, admin_token, "PO-T3", "acct-target", "Target")

        # Priya Nair sees only Target + Costco
        priya_token = _login(client, "priya.nair@acme-corp.com")
        r = client.get("/api/v1/exceptions", headers=_auth(priya_token))
        assert r.status_code == 200
        data = r.json()["data"]
        assert len(data) == 1
        assert data[0]["account_id"] == "acct-target"

    def test_exceptions_without_account_id_excluded_from_scoped(self, client):
        admin_token = _login(client, "marcus.webb@acme-corp.com")
        _create_exception_with_account(client, admin_token, "PO-NOACCT", None, None)

        james_token = _login(client, "james.ortiz@acme-corp.com")
        r = client.get("/api/v1/exceptions", headers=_auth(james_token))
        # Exception has no account_id, scoped user should not see it
        assert r.status_code == 200
        assert len(r.json()["data"]) == 0

    def test_account_id_in_summary_response(self, client):
        admin_token = _login(client, "marcus.webb@acme-corp.com")
        _create_exception_with_account(client, admin_token, "PO-SUM", "acct-walmart", "Walmart")

        r = client.get("/api/v1/exceptions", headers=_auth(admin_token))
        exc = r.json()["data"][0]
        assert exc["account_id"] == "acct-walmart"
        assert exc["account_name"] == "Walmart"

    def test_account_id_in_detail_response(self, client):
        admin_token = _login(client, "marcus.webb@acme-corp.com")
        exc_id = _create_exception_with_account(client, admin_token, "PO-DET", "acct-kroger", "Kroger")

        r = client.get(f"/api/v1/exceptions/{exc_id}", headers=_auth(admin_token))
        assert r.status_code == 200
        assert r.json()["account_id"] == "acct-kroger"
        assert r.json()["account_name"] == "Kroger"


# ===========================================================================
# JWT claims round-trip
# ===========================================================================


class TestJWTClaimsRoundTrip:
    """New JWT claims (title, avatar_initials, assigned_accounts) survive encode/decode."""

    def test_access_token_includes_title(self):
        token = create_access_token(
            sub="u1", email="u@x.com", name="U", roles=["analyst"], org="t1",
            title="CS Manager", avatar_initials="CM",
        )
        payload = _jwt_decode(token, _get_jwt_secret())
        assert payload["title"] == "CS Manager"
        assert payload["avatar_initials"] == "CM"

    def test_access_token_includes_assigned_accounts(self):
        token = create_access_token(
            sub="u1", email="u@x.com", name="U", roles=["analyst"], org="t1",
            assigned_accounts=["acct-a", "acct-b"],
        )
        payload = _jwt_decode(token, _get_jwt_secret())
        assert payload["assigned_accounts"] == ["acct-a", "acct-b"]

    def test_empty_assigned_accounts_omitted(self):
        """Empty list should not appear in JWT to keep payload small."""
        token = create_access_token(
            sub="u1", email="u@x.com", name="U", roles=["analyst"], org="t1",
        )
        payload = _jwt_decode(token, _get_jwt_secret())
        assert "assigned_accounts" not in payload

    def test_login_jwt_contains_claims(self, client):
        """End-to-end: login → JWT payload contains title and assigned_accounts."""
        r = client.post(
            "/api/auth/login",
            json={"email": "james.ortiz@acme-corp.com", "password": "password"},
        )
        token = r.json()["access_token"]
        payload = _jwt_decode(token, _get_jwt_secret())
        assert payload["title"] == "CS Analyst"
        assert payload["avatar_initials"] == "JO"
        assert payload["assigned_accounts"] == ["acct-walmart", "acct-kroger"]

    def test_me_endpoint_returns_new_fields(self, client):
        """GET /api/auth/me includes title, avatar_initials, visible_tabs, assigned_accounts."""
        token = _login(client, "sarah.chen@acme-corp.com")
        r = client.get("/api/auth/me", headers=_auth(token))
        assert r.status_code == 200
        data = r.json()
        assert data["title"] == "CS Manager"
        assert data["avatar_initials"] == "SC"
        assert "settings" in data["visible_tabs"]
        assert data["assigned_accounts"] == []


# ===========================================================================
# Auth switch edge cases
# ===========================================================================


class TestAuthSwitchEdgeCases:
    """POST /api/auth/switch — edge cases."""

    def test_switch_to_self_succeeds(self, client):
        token = _login(client, "marcus.webb@acme-corp.com")
        r = client.post(
            "/api/auth/switch",
            json={"email": "marcus.webb@acme-corp.com", "password": ""},
            headers=_auth(token),
        )
        assert r.status_code == 200
        assert r.json()["user"]["name"] == "Marcus Webb"

    def test_switch_unauthenticated(self, client):
        r = client.post(
            "/api/auth/switch",
            json={"email": "james.ortiz@acme-corp.com", "password": ""},
        )
        assert r.status_code == 401

    def test_switch_blocked_in_production(self, client, monkeypatch):
        monkeypatch.setenv("ASOE_ENV", "production")
        token = _login(client, "marcus.webb@acme-corp.com")
        r = client.post(
            "/api/auth/switch",
            json={"email": "james.ortiz@acme-corp.com", "password": ""},
            headers=_auth(token),
        )
        # JWT env=sandbox vs server env=production triggers env mismatch (403)
        assert r.status_code == 403
        monkeypatch.setenv("ASOE_ENV", "sandbox")

    def test_list_users_blocked_in_production(self, client, monkeypatch):
        monkeypatch.setenv("ASOE_ENV", "production")
        token = _login(client, "marcus.webb@acme-corp.com")
        r = client.get("/api/auth/users", headers=_auth(token))
        # JWT env=sandbox vs server env=production triggers env mismatch (403)
        assert r.status_code == 403
        monkeypatch.setenv("ASOE_ENV", "sandbox")

    def test_switch_returns_correct_visible_tabs(self, client):
        admin_token = _login(client, "marcus.webb@acme-corp.com")
        r = client.post(
            "/api/auth/switch",
            json={"email": "james.ortiz@acme-corp.com", "password": ""},
            headers=_auth(admin_token),
        )
        assert r.status_code == 200
        user = r.json()["user"]
        assert "settings" not in user["visible_tabs"]
        assert "inbox" in user["visible_tabs"]
        assert user["assigned_accounts"] == ["acct-walmart", "acct-kroger"]
