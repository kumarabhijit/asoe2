# Full Project Sequence Prompt
```text
Read architecture_v3.md, DESIGN.md, CLAUDE.md, the sample recipes, the sample skill, and tasks.md.
Work phase-by-phase only.
For the current response:
1. identify the exact phase being implemented
2. list affected files
3. implement the smallest viable increment
4. add tests first or together with code
5. preserve deterministic architecture
6. never invent new business logic outside recipes
7. never bypass Compliance Shadow
8. use Guidance / Outlines for all machine-consumed LLM outputs
9. stop and escalate when deterministic execution is not possible
Return a concise execution summary and test summary.
```
