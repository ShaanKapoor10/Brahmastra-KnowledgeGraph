## What and why

<!-- What changes, and the reason. Link the issue or ROADMAP item. -->

## How it was verified

<!-- Tests added or run, and what was measured. "CI passes" is not a verification. -->

## Checklist

- [ ] No secrets, real names or private recordings in the diff (tests use fake keys and invented names)
- [ ] Schema changes are backward compatible for one release (docs/OPERATIONS.md, *The migration rule*)
- [ ] Every new graph write is rebuildable from Postgres (`tests/test_graph_write_contract.py`)
- [ ] New logs use allowlisted fields only (`logs.py`)
- [ ] New dependencies: licence allowed, lock file updated
- [ ] Docs updated where behaviour changed
