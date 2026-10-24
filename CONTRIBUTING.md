# Contributing to SwiftBill

## Commit convention (Conventional Commits)

Repo hygiene is a graded concern in this project, so every commit follows:

```
<type>(<scope>): <short imperative summary>

<body: what changed and why, if not obvious>
```

**Types:** `feat` (new feature), `fix` (bug fix), `docs`, `test`,
`refactor`, `chore` (build/tooling), `perf`.

**Rules:**
- One logical change per commit. Docs-only and code changes never share
  a commit (use `docs:` vs `feat:`/`fix:` separately).
- Summary ≤ 72 chars, imperative mood ("add", "fix", not "added").
- Reference the scenario it affects, e.g. `test(sync): cover duplicate-push dedupe (S4)`.

**Branch naming:** `feat/<name>`, `fix/<name>`, `docs/<name>`, `test/<name>`.

## Pull-request checklist
- [ ] `python3 simulations/run_all.py` passes (13/13)
- [ ] New behaviour has a scenario in `simulations/run_all.py`
- [ ] Conflict-relevant changes update `docs/ARCHITECTURE_DECISIONS.md`
- [ ] New limitations added to `docs/KNOWN_LIMITATIONS.md`

## Local setup
```bash
cd offlinepos
python3 simulations/run_all.py   # runs the full simulation suite
```
No external dependencies — standard library only (Python 3.10+).
The central MySQL/MS SQL Server is simulated by
`offlinepos/central_db.py`; see ADR-005 before swapping in a real driver.
