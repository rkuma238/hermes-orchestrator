# Hermes — reference orchestrator for the Open Skill Protocol (OSP)

OSP is a small open protocol for **discovering, fetching, verifying, and
executing** remote "skills" on demand, instead of statically installing every
tool an agent might ever need. Full protocol spec: [`spec/SPEC.md`](spec/SPEC.md).
Manifest schema: [`spec/skill-manifest.schema.json`](spec/skill-manifest.schema.json).
Licensed under [Apache-2.0](LICENSE) — see [CONTRIBUTING.md](CONTRIBUTING.md)
before opening a PR.

## Architecture

```
                     ┌─────────────────────────── Envoy gateway :10000 ───────────────────────────┐
                     │  ext_authz -> /internal/authz on every request except POST /accounts        │
                     │  and /dashboard (public, unauthenticated)                                   │
  Orchestrator ──────┤                                                                              │
  (hermes/)          │   /            ──────────────► registry_service :8079  (discovery, manifests,│
                      │                                 accounts, publish, dashboard, invocation log)│
                      │   /partner/*  ──────────────► partner_service  :8082  (independent backend) │
                      │   /labs/*     ──────────────► labs_service     :8083  (independent backend) │
                      └──────────────────────────────────────────────────────────────────────────────┘
```

One gateway, one authentication model (`envoy/backends.yaml` +
`scripts/generate_envoy_config.py`), N independently-operated skill backends.
A skill's *manifest* always lives with the registry that owns its discovery
entry; its *payload* can be routed to a completely different backend purely
by URL prefix — see `partner-currency-convert` and `labs-reverse-text` for
worked examples. Onboarding a new registry is "add an entry to
`backends.yaml`, regenerate, restart Envoy" — no hand-edited routes.

- **`spec/`** — the protocol itself (manifest schema + full write-up of the
  discover → authenticate → authorize → fetch → verify → execute flow).
- **`envoy/`** — the gateway. `backends.yaml` is the declarative source of
  truth; `envoy.yaml` is generated from it.
- **`registry_server/`** — the primary backend: accounts/API keys (SQLite),
  discovery, manifest/payload serving, publishing, invocation telemetry, and
  the publisher dashboard (mounted at `/dashboard`).
- **`partner_service/`, `labs_service/`** — example independent skill
  backends, each just a few lines calling `osp_common.skill_backend`.
- **`osp_common/`** — the visibility/ACL check shared by every backend, so
  it has exactly one implementation instead of being copy-pasted.
- **`hermes/`** — the reference orchestrator: authenticates to the gateway,
  discovers skills, verifies payload integrity by checksum before ever
  running them, enforces a deny-by-default capability policy, executes in an
  isolated subprocess, and wraps discovered skills as LangChain
  `StructuredTool`s.
- **`dashboard/`** — static publisher UI (register, publish, view your
  skills + invocation log), served same-origin through the gateway.

## Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
brew install envoy   # or your platform's equivalent

# one terminal each:
uvicorn registry_server.main:app --port 8079
uvicorn partner_service.main:app --port 8082
uvicorn labs_service.main:app --port 8083
envoy -c envoy/envoy.yaml

# then:
python -m examples.run_end_to_end
```

Dashboard: open `http://127.0.0.1:10000/dashboard/` — register an account,
publish a skill, watch its checksum and invocation log.

## Adding a new skill registry

1. Write a backend (see `partner_service/main.py` for the ~6-line pattern
   using `osp_common.skill_backend.make_skill_backend_app`) with its own
   `skills_store/`.
2. Add an entry to `envoy/backends.yaml`: name, host, port, `route_prefix`.
3. `python -m scripts.generate_envoy_config` and restart Envoy.

No changes to any other backend or to hand-written Envoy routes — that's the
whole point of the generator (see `scripts/generate_envoy_config.py`).

## Using discovered skills as LangChain tools

```python
import httpx
from hermes import HermesOrchestrator
from hermes.langchain_tool import build_langchain_tools

account = httpx.post("http://127.0.0.1:10000/accounts", json={"name": "my-agent"}).json()
orchestrator = HermesOrchestrator(
    "http://127.0.0.1:10000",           # the gateway, not a registry directly
    api_key=account["api_key"],
    allowed_capabilities={"net:https://api.example.com/*"},  # deployment policy
)
tools = build_langchain_tools(orchestrator)  # discovers + wraps every authorized skill

# tools is a list[StructuredTool] — hand it straight to a LangChain agent
```

Each tool fetches, verifies, and executes its skill's payload only when the
agent actually calls it — nothing is downloaded up front.

## Security model (read before pointing this at untrusted skills or accounts)

- **Authentication is Envoy's job, for every request.** `ext_authz` calls
  `/internal/authz` (never externally routable) on everything except the
  signup route and static dashboard assets. The registry trusts the
  `x-account-id` header completely — which is only safe because none of the
  backends are reachable except through the gateway.
- **Authorization is the registry's job.** A skill is `public` (any
  authenticated account) or `private` (owner + `allowed_accounts` only).
  Discovery and manifest/payload fetch both 404 — not 403 — on
  unauthorized private skills, so a caller can't distinguish "doesn't exist"
  from "exists but you can't see it."
- **Integrity, not obscurity.** Every payload is sha256-verified against its
  manifest *by the orchestrator, right before executing it* — not by the
  gateway. Checksum/schema verification has to happen at the point that acts
  on the data; doing it at the gateway would just move the trust problem
  back to trusting the gateway. See spec/SPEC.md's design principles.
- **v0.1's sandbox is process isolation, not a hard security boundary.**
  `SubprocessSandboxRunner` runs each call in a fresh, environment-scrubbed
  `python -I -S` subprocess with a timeout and a memory rlimit — good enough
  for skills you trust during development, **not** a substitute for
  container/gVisor/Firecracker or a Wasm sandbox against arbitrary
  third-party code.

## Tests

```bash
pytest tests/ -v
```

Spins up all three backends + a real Envoy instance (on dedicated test
ports) and covers: unauthenticated requests rejected, `/internal/authz`
blocked externally, discovery/invocation through the skill router across all
three backends, input/output schema validation, capability grant/deny,
tampered-payload rejection, private-skill ACL enforcement across two
accounts, and invocation telemetry.

## Status / non-goals

No payments or licensing layer yet (see `spec/SPEC.md`'s Non-goals) — this is
the open discover/authenticate/authorize/fetch/verify/execute protocol and
its Python reference implementation, aimed at eventually upstreaming the
LangChain integration (`hermes/langchain_tool.py`) once it's had more
real-world use.
