# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) —
though everything before `1.0.0` should be treated as unstable: the
manifest schema, protocol version (`osp_version`), and Python APIs may still
change without a deprecation period.

## [Unreleased]

## [0.1.0] - 2026-09-25

Initial reference implementation of the Open Skill Protocol (OSP).

### Added
- Protocol spec (`spec/SPEC.md`) and manifest JSON Schema
  (`spec/skill-manifest.schema.json`), `osp_version: "0.1"`.
- Reference registry (`registry_server/`): accounts/API keys, discovery,
  manifest/payload serving, publishing, invocation telemetry, and a static
  publisher dashboard (`dashboard/`).
- Envoy gateway (`envoy/`) enforcing authentication on every request via
  `ext_authz`, generated from a declarative multi-backend list
  (`envoy/backends.yaml` → `scripts/generate_envoy_config.py`) rather than
  hand-edited routes.
- Two example independent skill backends (`partner_service/`,
  `labs_service/`) proving the gateway's path-based skill router and
  multi-registry onboarding.
- `osp_common`: the visibility/ACL authorization check shared by every
  backend, and a factory (`skill_backend.py`) for adding new minimal
  backends in a few lines.
- Python orchestrator (`hermes/`): discovery client, checksum-verifying
  payload fetch, deny-by-default capability enforcement, an isolated
  subprocess sandbox (`SubprocessSandboxRunner`), and a LangChain
  `StructuredTool` adapter (`hermes/langchain_tool.py`).
- Packaging (`pyproject.toml`) for `hermes` as an installable `osp-hermes`
  package, independent of the rest of the reference stack.
- Test suite covering unauthenticated-request rejection, cross-backend
  skill routing, schema validation, capability grant/deny, tampered-payload
  rejection, cross-account private-skill ACL enforcement, and invocation
  telemetry — run against a real multi-backend + Envoy stack, not mocks.

[Unreleased]: https://github.com/rkuma238/hermes-orchestrator/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/rkuma238/hermes-orchestrator/releases/tag/v0.1.0
