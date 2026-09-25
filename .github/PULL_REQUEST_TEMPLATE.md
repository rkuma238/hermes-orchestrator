## What does this change and why

<!-- Link the issue if there is one. -->

## Type of change

- [ ] Reference-implementation change (bug fix / improvement)
- [ ] Protocol change (spec/SPEC.md or skill-manifest.schema.json) — was this discussed in an issue first?

## Checklist (see CONTRIBUTING.md)

- [ ] `pytest tests/ -v` passes locally
- [ ] If `envoy/backends.yaml` changed, `envoy/envoy.yaml` was regenerated (`python -m scripts.generate_envoy_config`) and committed
- [ ] If the manifest shape changed, `spec/skill-manifest.schema.json` and `skillward/manifest.py` were updated together
- [ ] New capability/visibility/auth behavior has a test that fails without the fix
- [ ] `ruff check .` passes
