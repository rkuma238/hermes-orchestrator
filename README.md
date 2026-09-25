# Skillward — reference orchestrator for the Skillward Protocol

[![CI](https://github.com/rkuma238/skillward/actions/workflows/ci.yml/badge.svg)](https://github.com/rkuma238/skillward/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

Skillward is a small open protocol for **discovering, fetching, verifying, and
executing** remote "skills" on demand, instead of statically installing every
tool an agent might ever need. Full protocol spec: [`spec/SPEC.md`](spec/SPEC.md).
Manifest schema: [`spec/skill-manifest.schema.json`](spec/skill-manifest.schema.json).
Licensed under [Apache-2.0](LICENSE) — see [CONTRIBUTING.md](CONTRIBUTING.md)
before opening a PR.

## Why would I use this?

Normally, if you want an AI agent to do something new — check the weather,
convert a currency, query a database — you write that code yourself, install
a plugin, or hope your framework already has it built in. That means every
agent you build carries around every tool it might ever need, whether it
uses it or not, and there's no real way to know whether a tool you didn't
write yourself is safe to run.

Skillward flips that around. Your agent asks a catalog "does anything do
X?", gets back a small piece of code, checks a cryptographic fingerprint to
make sure it's exactly what was published (not something swapped out along
the way), runs it in a locked-down sandbox, and gets the result back — all
on the fly, the first time it's actually needed. Nothing is pre-installed.
Nothing gets network or environment access it wasn't explicitly granted. And
it plugs into whichever agent framework you already use, so you don't have
to change how you build agents to get this.

Use it if:

- You want your agent to pick up new abilities without you writing or
  pre-installing them.
- You don't fully trust where a tool came from, and want it verified and
  sandboxed before it ever runs.
- You're building on more than one agent framework and don't want a
  different "how do I load a tool" story for each one.
- You want to let other people or teams publish skills your agents can use,
  without handing them a backdoor into your systems.

## Architecture

```
                    ┌───────────────────────── Envoy gateway :10000 ─────────────────────────┐
                    │ ext_authz -> /internal/authz on every request except POST /accounts     │
                    │ and /dashboard (public, unauthenticated)                                │
 Orchestrator ──────┤                                                                          │
 (skillward/)       │  /            ──────────► registry_service :8079  (discovery, manifests,│
                    │                             accounts, publish, dashboard, invocation log)│
                    │  /partner/*   ──────────► partner_service  :8082  (independent backend)  │
                    │  /labs/*      ──────────► labs_service     :8083  (independent backend)  │
                    └──────────────────────────────────────────────────────────────────────────┘
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
  backends, each just a few lines calling `skillward_common.skill_backend`.
- **`skillward_common/`** — the visibility/ACL check shared by every backend, so
  it has exactly one implementation instead of being copy-pasted.
- **`skillward/`** — the reference orchestrator: authenticates to the gateway,
  discovers skills, verifies payload integrity by checksum before ever
  running them, enforces a deny-by-default capability policy, executes in an
  isolated subprocess, and wraps discovered skills as native tool objects for
  LangChain, LlamaIndex, CrewAI, AutoGen, Google ADK, or OpenAI.
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
   using `skillward_common.skill_backend.make_skill_backend_app`) with its own
   `skills_store/`.
2. Add an entry to `envoy/backends.yaml`: name, host, port, `route_prefix`.
3. `python -m scripts.generate_envoy_config` and restart Envoy.

No changes to any other backend or to hand-written Envoy routes — that's the
whole point of the generator (see `scripts/generate_envoy_config.py`).

## Using discovered skills with your agent framework

Every adapter does the same thing: discover skills from the registry, and
hand your framework's native tool object back — verification, capability
enforcement, and sandboxed execution all still happen inside the
orchestrator, invisibly, the first time the agent actually calls the tool.

```python
import httpx
from skillward import SkillwardOrchestrator

account = httpx.post("http://127.0.0.1:10000/accounts", json={"name": "my-agent"}).json()
orchestrator = SkillwardOrchestrator(
    "http://127.0.0.1:10000",  # the gateway, not a registry directly
    api_key=account["api_key"],
    allowed_capabilities={"net:https://api.example.com/*"},  # deployment policy
)
```

**LangChain** (`pip install skillward[langchain]`)
```python
from skillward.langchain_tool import build_langchain_tools

tools = build_langchain_tools(orchestrator)  # list[StructuredTool]
```

**LlamaIndex** (`pip install skillward[llamaindex]`)
```python
from skillward.llamaindex_tool import build_llamaindex_tools

tools = build_llamaindex_tools(orchestrator)  # list[FunctionTool]
```

**CrewAI** (`pip install skillward[crewai]`)
```python
from skillward.crewai_tool import build_crewai_tools

tools = build_crewai_tools(orchestrator)  # list[BaseTool]
```

**AutoGen** (`pip install skillward[autogen]`)
```python
from skillward.autogen_tool import build_autogen_tools

tools = build_autogen_tools(orchestrator)  # list[FunctionTool]
```

**Google ADK** (`pip install skillward[google-adk]`)
```python
from skillward.google_adk_tool import build_google_adk_tools

tools = build_google_adk_tools(orchestrator)  # list[FunctionTool]
```

**OpenAI** (Responses API — current/recommended; no extra dependency)
```python
from skillward.openai_tool import build_openai_responses_toolset

toolset = build_openai_responses_toolset(orchestrator)
# toolset.tools -> pass straight to client.responses.create(tools=...)
# toolset.dispatch(name, arguments_json) -> actually run the matching skill
```
Chat Completions' nested tool shape is also available as
`build_openai_chat_completions_toolset`. The Assistants API isn't
supported — it was sunset on 2026-08-26 with no migration window.

> **Don't install `crewai` and `google-adk` in the same environment** — as of
> the versions this was built against, they pull in conflicting protobuf
> versions (crewai via its `chromadb` dependency). Every other combination is
> fine. This is exactly why each adapter is its own optional extra rather
> than a single bundled `[all]`.

AutoGen and Google ADK generate a tool's schema by inspecting a real Python
function's signature rather than accepting an explicit schema object, so
those two adapters synthesize a function with a genuine `inspect.Signature`
matching the skill's `input_schema` at discovery time (see
`skillward/_dynamic_function.py`) — there's nothing to configure, it's just
worth knowing the schema isn't hand-written per skill.

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
its Python reference implementation, with adapters for LangChain,
LlamaIndex, CrewAI, AutoGen, Google ADK, and OpenAI, aimed at eventually
proposing the most-used one(s) back to their respective ecosystems once
they've had more real-world use.
