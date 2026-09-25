# Contributing

Thanks for considering a contribution. This repo has two different kinds of
surface area, and it's worth being clear about which one a change touches:

- **The protocol** (`spec/SPEC.md`, `spec/skill-manifest.schema.json`) —
  changes here affect every implementation, not just this one. Propose
  protocol changes as an issue first, before a PR, so the design gets
  discussed independent of any particular code change.
- **The reference implementation** (`registry_server/`, `skillward/`,
  `envoy/`, `partner_service/`, `labs_service/`, `skillward_common/`,
  `dashboard/`) — one working implementation of the protocol above. Bug
  fixes and improvements here can go straight to a PR.

## Dev setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install -e .          # installs the skillward package itself, editable

brew install envoy        # macOS; see https://www.envoyproxy.io/docs/envoy/latest/start/install
                           # or use func-e (https://func-e.io) on Linux/CI
```

## Running the full stack locally

```bash
# one terminal each
uvicorn registry_server.main:app --port 8079
uvicorn partner_service.main:app --port 8082
uvicorn labs_service.main:app --port 8083
envoy -c envoy/envoy.yaml

# then
python -m examples.run_end_to_end
```

Dashboard: `http://127.0.0.1:10000/dashboard/`.

## Running tests

```bash
pytest tests/ -v
```

The suite spins up all three backends and a real Envoy instance on isolated
test ports (`18079`/`18082`/`18083`/`18010`, distinct from the dev ports
above, so it can run alongside a manually-running dev stack) and an isolated
temp SQLite DB + skills_store per run — it does not touch
`registry_server/skills_store/`'s committed example data. If you add a test
that publishes a skill, use a unique skill id per test (see
`test_private_skill_hidden_from_other_accounts` for the pattern) so reruns
stay idempotent.

## Adding a new skill backend to the gateway

Don't hand-edit `envoy/envoy.yaml` — it's generated. Instead:

1. Add a backend under `envoy/backends.yaml` (name, host, port,
   `route_prefix`).
2. `python -m scripts.generate_envoy_config`
3. Commit both `backends.yaml` and the regenerated `envoy.yaml`.

See `labs_service/` for the minimal pattern (a few lines calling
`skillward_common.skill_backend.make_skill_backend_app`).

## Code style

- No comments explaining *what* code does — names should do that. Comments
  are for non-obvious *why* (a security invariant, a workaround, a protocol
  requirement).
- Don't add error handling for cases that can't happen; don't add
  configuration knobs for hypothetical future needs.
- Match the existing security posture: anything that decides *whether a
  caller may see or run something* belongs in `skillward_common` or the owning
  backend, not duplicated per-backend. See `spec/SPEC.md`'s design
  principles before changing where a check lives.

## Before opening a PR

- [ ] `pytest tests/ -v` passes locally
- [ ] If you touched `envoy/backends.yaml`, `envoy/envoy.yaml` was
      regenerated and committed alongside it
- [ ] If you touched the manifest shape, `spec/skill-manifest.schema.json`
      and the `skillward/manifest.py` pydantic models were updated together
- [ ] New capability/visibility/auth behavior has a test that would fail
      without the fix (see the existing ACL and capability-denial tests for
      the pattern)
