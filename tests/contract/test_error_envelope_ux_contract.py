"""UX/a11y contract on the API error envelope.

Companion to `docs/test-strategy/ux-api-contract.md` and the
asoe-ui-side strategy at
`asoe-ui/docs/test-strategy/UX_ACCESSIBILITY.md`.

The UI binds `error.message` directly into the `StatusAnnouncer`
aria-live region and the Toast component. A null / empty / raw-
enum message produces a useless screen-reader announcement.

This test walks every `raise ASOEError(...)` call site in
`api/` and asserts the message contract:

  1. Every site supplies a `code=` and a `message=` kwarg.
  2. When `message=` is a pure string literal, it is non-empty
     and ends with terminal punctuation (`.`, `?`, `!`).
  3. When `message=` is an f-string (JoinedStr), the trailing
     constant part — if any — ends with terminal punctuation, or
     the f-string ends with a `FormattedValue` whose value is
     bracketed by punctuation (e.g. f"Case {id} not found.").
  4. A string-literal `message=` does not include the `code=`
     value verbatim (avoids screen-reader stutter).

Sites whose `message=` is constructed dynamically (BinOp, Call)
are exempt from the terminal-punctuation rule — those are
typically `str(exc)` wrappers — but they MUST still pass the
presence check (rule 1).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Iterable

API_ROOT = Path(__file__).resolve().parent.parent.parent / "api"


def _iter_asoeerror_calls(tree: ast.AST) -> Iterable[ast.Call]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "ASOEError":
            yield node


def _kwarg(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _string_literal(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _ends_with_terminal_punctuation(s: str) -> bool:
    stripped = s.rstrip()
    return bool(stripped) and stripped[-1] in {".", "?", "!"}


def _check_terminal_punctuation(node: ast.expr) -> tuple[bool, str]:
    """Return (ok, description) for the terminal-punctuation rule.

    * Pure string literal → check the literal directly.
    * JoinedStr that ends with a Constant → check the trailing
      Constant. (Sentences like f"Foo {x}." land here.)
    * JoinedStr that ends with a FormattedValue → cannot
      statically prove, accept. (Sentences like f"Case {id}" —
      the consumer is expected to wrap the substitution in
      punctuation when it matters.)
    * Anything else → accept (dynamic — exempt from this rule).
    """
    lit = _string_literal(node)
    if lit is not None:
        if _ends_with_terminal_punctuation(lit):
            return True, ""
        return False, f"literal {lit!r} missing terminal punctuation"

    if isinstance(node, ast.JoinedStr) and node.values:
        last = node.values[-1]
        last_lit = _string_literal(last)
        if last_lit is not None:
            if _ends_with_terminal_punctuation(last_lit):
                return True, ""
            return False, (
                f"f-string trailing literal {last_lit!r} missing "
                "terminal punctuation"
            )
        # Trailing FormattedValue — accept.
        return True, ""

    # BinOp / Call / Name — dynamic, accept.
    return True, ""


def _collect_sites() -> list[tuple[Path, int, ast.expr | None, ast.expr | None]]:
    out: list[tuple[Path, int, ast.expr | None, ast.expr | None]] = []
    for py in API_ROOT.rglob("*.py"):
        if py.name == "errors.py" and py.parent == API_ROOT:
            continue
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for call in _iter_asoeerror_calls(tree):
            out.append(
                (py, call.lineno, _kwarg(call, "code"), _kwarg(call, "message"))
            )
    return out


SITES = _collect_sites()


def _fmt(path: Path, lineno: int) -> str:
    return f"{path.relative_to(API_ROOT.parent)}:{lineno}"


def test_walker_discovers_a_meaningful_number_of_sites() -> None:
    assert len(SITES) >= 10, (
        f"AST walker found only {len(SITES)} ASOEError(...) raise sites. "
        "Walker is likely broken — investigate."
    )


def test_every_site_supplies_code_and_message_kwargs() -> None:
    violations: list[str] = []
    for path, lineno, code_node, msg_node in SITES:
        if code_node is None:
            violations.append(f"{_fmt(path, lineno)}: missing code= kwarg")
        if msg_node is None:
            violations.append(f"{_fmt(path, lineno)}: missing message= kwarg")
    assert violations == [], "\n".join(violations)


def test_string_literal_messages_are_non_empty() -> None:
    violations: list[str] = []
    for path, lineno, _code_node, msg_node in SITES:
        lit = _string_literal(msg_node)
        if lit is None:
            continue
        if not lit.strip():
            violations.append(f"{_fmt(path, lineno)}: message= is empty")
    assert violations == [], "\n".join(violations)


def test_messages_end_with_terminal_punctuation_when_statically_provable() -> None:
    violations: list[str] = []
    for path, lineno, _code_node, msg_node in SITES:
        if msg_node is None:
            continue
        ok, desc = _check_terminal_punctuation(msg_node)
        if not ok:
            violations.append(f"{_fmt(path, lineno)}: {desc}")
    assert violations == [], (
        "ASOEError messages must read as a sentence so the UI aria-live "
        "announcement is intelligible:\n" + "\n".join(violations)
    )


def test_literal_message_does_not_echo_code_verbatim() -> None:
    violations: list[str] = []
    for path, lineno, code_node, msg_node in SITES:
        code = _string_literal(code_node)
        msg = _string_literal(msg_node)
        if not (code and msg):
            continue
        if code in msg:
            violations.append(
                f"{_fmt(path, lineno)}: message={msg!r} echoes code={code!r} — "
                "drop the code from the message; the UI labels it separately."
            )
    assert violations == [], "\n".join(violations)
