# Documentation Update Prompt

```text
Read architecture_v3.md, DESIGN.md, CLAUDE.md, and tasks.md in full before making any changes.

Also read each document you are about to update so you understand its current state.

Target documents (update only the ones relevant to what has changed):
- README.md              — engineer cookbook; audience: novice engineers onboarding to the project
- docs/AUDITOR_GUIDE.md  — audit controls reference; audience: auditors and operators
- docs/adr/*.md          — architecture decision records; audience: architects and senior engineers
- tasks.md               — phase checklist; mark completed items with [x]
- prompts/phase_*.md     — phase-specific build prompts; add new phases as needed
- (any other *.md added in future)

Excluded from doc updates (owned by product, not engineering):
- docs/specs/*.md        — PO product specs; do not modify during code changes

Rules:
1. Update only what has actually changed in the codebase since the last doc update.
   Do not rewrite sections that are still accurate.
2. Keep the existing structure and headings unless a structural change is strictly required.
3. All code examples must match the current source (contracts/models.py, constraints/specs.py,
   hardening/, compliance/shadow.py, etc.). Verify before writing.
4. Do not add speculative sections, hypothetical features, or forward-looking content.
5. Do not remove content that is still accurate and useful to the audience.
6. If the change touches code or config (not just prose), run python -m pytest to confirm
   tests still pass. For prose-only edits, skip the test run.
7. Commit with a message of the form: "docs: update <filename> — <one-line reason>"

README.md-specific guidance (novice engineer audience):
8. Write for someone who has never seen this codebase. Assume no prior knowledge
   of the Skill-Shadow-Recipe architecture, LangGraph, or the domain.
9. Every setup step must be copy-pasteable. Include the exact commands to clone,
   install, configure, run, and test. Do not assume the reader knows which
   flags, env vars, or config files are needed.
10. Define acronyms and domain terms on first use (e.g., "OMS (Order Management
    System)", "EDI 850 (electronic purchase order)").
11. Use short sentences. Prefer bullet lists over dense paragraphs.
12. For each module or directory mentioned, include a one-line plain-English
    description of what it does and why it exists.
13. Include a "Common Problems" or "Troubleshooting" section for errors a new
    engineer is likely to hit on first setup (missing deps, env vars, DB init).

Change discipline:
- Minimal and incremental. One section or one document at a time if possible.
- If a rename or restructure is not strictly necessary, leave it untouched.
- If architectural intent is unclear, stop and ask for clarification.
```
