# UX / accessibility API contract

Companion to `asoe-ui/docs/test-strategy/UX_ACCESSIBILITY.md`.
Records the API-side contracts the UI relies on to render
accessible status states, screen-reader announcements, and
clutter-free detail surfaces.

Most UX/a11y testing lives in `asoe-ui` (component axe sweeps,
focus management, route-level axe). But two contracts are
unilateral to the backend and worth locking here:

## 1. Error envelope carries a human-readable message

Spec: `api/errors.py::ErrorEnvelope`. Every error response uses
the shape

```json
{ "error": {
    "code": "STRING_CONSTANT",
    "message": "Sentence-case, period-terminated human message.",
    "trace_id": "...",
    "details": { ... } } }
```

The UI binds `error.message` to the toast and the
`StatusAnnouncer` aria-live region. If `message` is null,
empty, or a raw enum like `"VALIDATION_FAILED"`, the announcer
emits a useless announcement and the toast shows raw stack-trace
shrapnel — both regress the screen-reader UX. The contract is:

  * `message` is non-empty.
  * `message` is sentence-case (first character upper or a digit
    / quote / brand name) and ends with a period.
  * `message` does NOT include the `code` string verbatim — the
    UI labels the code separately and a repeated code reads as
    a stutter in NVDA / VoiceOver.

A pytest fixture
(`tests/contract/test_error_envelope_ux_contract.py`) walks
every concrete `ASOEError` subclass and a representative error
sample per HTTP route, asserting the message contract.

## 2. Status / lifecycle / verdict values have display labels

The UI's `useHealth` hook fetches the live set of valid status,
lifecycle, intent, and verdict strings from `/api/v1/health`
(Guardrail #2). The UI renders these strings directly into
badges and headings — they MUST be display-safe:

  * length ≤ 32 characters (a longer string wraps the badge
    component and breaks the queue row alignment);
  * uppercase snake_case (the UI's `lifecycleVariant()` and
    `verdictVariant()` mappers expect this shape; mixed case
    would silently fall to the `default` variant).

Same fixture above asserts these constraints by reading the
canonical enums in `contracts/models.py`.

## 3. No frontend-displayed payload field is HTML or markdown

The UI renders backend strings as text via React (auto-escaped),
so an HTML-formatted backend message would render as literal
`&lt;` characters — which screen readers spell out, polluting
the announcement. Any field on a payload reachable from
`ExceptionDetail`, `CaseRecord`, or the analysis sections must
be plain text. The fixture greps the relevant payload-builder
modules for inline `<` / `>` characters in string literals and
flags them.

## What this contract does NOT cover

  * Render-time accessibility — owned by
    `asoe-ui/tests/accessibility/` and
    `asoe-ui/tests/browser/a11y-route-sweep.spec.ts`.
  * Layout / clutter — owned by the asoe-ui source-grep
    invariants in
    `asoe-ui/tests/architectural/ux_clutter_invariants.test.ts`.
  * Color contrast — owned by
    `asoe-ui/tests/accessibility/design_tokens_contrast.test.ts`.

Backend changes that touch this contract (a new exception
class, a renamed lifecycle state, an enriched payload field)
must run the asoe-ui browser-e2e workflow on the matching feature
branch to surface compositional regressions before merge.
