"""DoR #6 — outbox reconcile scheduler (opt-in periodic worker).

The scheduler is OFF unless ASOE_OUTBOX_RECONCILE_INTERVAL_S is set. The loop
calls reconcile each cycle, survives a failing cycle, and (with tenant_ids)
reconciles per tenant. Deterministic — a fake sleep + max_cycles bound the loop.
"""

from __future__ import annotations

import asyncio

import pytest

from orchestration import outbox_scheduler as sched


def test_env_parsing(monkeypatch):
    monkeypatch.delenv("ASOE_OUTBOX_RECONCILE_INTERVAL_S", raising=False)
    assert sched.reconcile_interval_from_env() is None
    monkeypatch.setenv("ASOE_OUTBOX_RECONCILE_INTERVAL_S", "0")
    assert sched.reconcile_interval_from_env() is None
    monkeypatch.setenv("ASOE_OUTBOX_RECONCILE_INTERVAL_S", "not-a-number")
    assert sched.reconcile_interval_from_env() is None
    monkeypatch.setenv("ASOE_OUTBOX_RECONCILE_INTERVAL_S", "30")
    assert sched.reconcile_interval_from_env() == 30.0


def test_loop_runs_reconcile_each_cycle():
    calls = []

    async def _sleep(_):  # no real waiting
        return None

    reports = asyncio.run(sched.run_reconcile_loop(
        0.0, max_cycles=3,
        reconcile=lambda **kw: calls.append(kw) or {"retried": 0},
        sleep=_sleep,
    ))
    assert len(reports) == 3
    assert all(c == {"tenant_id": None} for c in calls)


def test_loop_reconciles_per_tenant():
    calls = []

    async def _sleep(_):
        return None

    asyncio.run(sched.run_reconcile_loop(
        0.0, tenant_ids=["t1", "t2"], max_cycles=1,
        reconcile=lambda **kw: calls.append(kw["tenant_id"]) or {},
        sleep=_sleep,
    ))
    assert calls == ["t1", "t2"]


def test_loop_survives_a_failing_cycle():
    n = {"i": 0}

    def _reconcile(**kw):
        n["i"] += 1
        if n["i"] == 1:
            raise RuntimeError("boom")
        return {"ok": True}

    async def _sleep(_):
        return None

    reports = asyncio.run(sched.run_reconcile_loop(
        0.0, max_cycles=2, reconcile=_reconcile, sleep=_sleep,
    ))
    # First cycle raised (swallowed, no report); second succeeded.
    assert reports == [{"ok": True}]
    assert n["i"] == 2


def test_start_if_configured_is_noop_when_disabled(monkeypatch):
    monkeypatch.delenv("ASOE_OUTBOX_RECONCILE_INTERVAL_S", raising=False)

    async def _check():
        return sched.start_if_configured()

    assert asyncio.run(_check()) is None
