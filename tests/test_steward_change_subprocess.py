"""End-to-end subprocess tests for the steward CLI.

The in-process tests at tests/test_steward_change_workflow.py call
``scripts.steward_change.main()`` directly. That covers the business
logic but doesn't catch argparse-shape regressions, missing entry
points, or import-time errors that only surface when a real shell
invokes ``python -m scripts.steward_change``.

This file fills the gap with subprocess-level smoke tests against
the committed seed. The tests are read-only / validate-only so they
do not touch the seed YAML or the committed generated constants.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _run(*args: str, cwd: Path = REPO_ROOT) -> subprocess.CompletedProcess:
    """Invoke the CLI as a user would, capturing stdout/stderr."""
    return subprocess.run(
        [sys.executable, "-m", "scripts.steward_change", *args],
        cwd=cwd, capture_output=True, text=True, timeout=30,
    )


def test_module_is_invokable_via_python_m():
    """``python -m scripts.steward_change --help`` must succeed.
    Catches import-time errors and missing __main__ paths."""
    result = _run("--help")
    assert result.returncode == 0, (
        f"stderr: {result.stderr}\nstdout: {result.stdout}"
    )
    assert "add-intent" in result.stdout
    assert "add-supergroup" in result.stdout
    assert "deprecate-intent" in result.stdout
    assert "validate" in result.stdout


def test_no_subcommand_prints_usage_and_errors():
    """argparse `required=True` on the subcommand should reject a
    bare invocation with a usage message and a non-zero exit code."""
    result = _run()
    assert result.returncode != 0
    assert "usage" in (result.stderr + result.stdout).lower()


def test_validate_against_committed_seed_passes():
    """``validate`` should be clean against the committed state of
    main. If this fails, either the YAML has drifted from the
    generated constants (`make taxonomy-gen` not run after a YAML
    edit) or the seed has an invariant violation."""
    result = _run("validate")
    assert result.returncode == 0, (
        f"validate failed against committed seed:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "OK" in result.stdout


def test_add_intent_help_advertises_required_args():
    """Each subcommand surfaces its required args via --help so the
    steward can self-serve at the terminal."""
    result = _run("add-intent", "--help")
    assert result.returncode == 0
    for arg in ("--code", "--supergroup", "--description"):
        assert arg in result.stdout, (
            f"add-intent --help missing {arg!r}; got:\n{result.stdout}"
        )


@pytest.mark.parametrize("subcmd", ["add-intent", "add-supergroup",
                                    "deprecate-intent", "validate"])
def test_each_subcommand_help_succeeds(subcmd: str):
    """Every advertised subcommand must respond to --help. A subcommand
    listed in the top-level help that crashes on --help is a real
    bug; this parametrised test catches it."""
    result = _run(subcmd, "--help")
    assert result.returncode == 0, (
        f"{subcmd} --help failed:\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )


def test_unknown_subcommand_rejected():
    result = _run("delete-everything")
    assert result.returncode != 0
    assert "invalid choice" in (result.stderr + result.stdout).lower() or \
           "unrecognized" in (result.stderr + result.stdout).lower()
