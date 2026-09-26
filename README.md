# Skillward — reference orchestrator for the Skillward Protocol

[![CI](https://github.com/rkuma238/skillward/actions/workflows/ci.yml/badge.svg)](https://github.com/rkuma238/skillward/actions/workflows/ci.yml)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](LICENSE)

Skillward is a small open protocol for **discovering, fetching, verifying, and
executing** remote "skills" **served over plain HTTPS** on demand, instead of
statically installing every tool an agent might ever need. Full protocol
spec: [`spec/SPEC.md`](spec/SPEC.md). Manifest schema:
[`spec/skill-manifest.schema.json`](spec/skill-manifest.schema.json).
Licensed under [Apache-2.0](LICENSE) — see [CONTRIBUTING.md](CONTRIBUTING.md)
before opening a PR.

**Read "How this relates to MCP and Agent Skills" below before assuming this
replaces either** — it doesn't, and it isn't interoperable with them.

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

## For platform/security teams: the problem with local skill installs

Everything above is the individual-developer pitch. There's a separate,
sharper reason a platform or security team would care: **locally-installed
skills — a folder on someone's laptop, a script cloned from some repo, a
`SKILL.md` dropped into an agent's skills directory — have no central
management story at all**, and that's a real operational and security
problem once more than one person is involved.

Concretely, with skills living as local files:

1. **No visibility.** There's no way to know what skills are actually
   running across an org, who installed them, or when they last changed.
2. **No integrity guarantee.** A skill is just files on disk. Nothing ties
   "the version someone reviewed" to "the version that's actually
   executing" — a compromised dependency, a malicious contributor, or a
   file quietly modified after review all look identical from the outside.
3. **No access control.** Anyone with filesystem access can drop a skill
   into their own skills directory and start using it. There's no way to
   say "only the finance team can use the skill that touches the ledger
   API" or "this one isn't approved for production yet."
4. **No revocation.** If a skill turns out to be broken or malicious, there
   is no "pull it back" — someone has to notice, then track down and clean
   up every machine it might be sitting on.
5. **No audit trail.** When something goes wrong, there's no record of
   which skill ran, for whom, with what input, or whether it succeeded.
6. **Version drift.** Different machines end up running different versions
   of "the same" skill, because there's no single source of truth anyone is
   actually pulling from.
7. **No skill SDLC.** A file on disk has no lifecycle: no way to publish a
   new version without touching every machine that has the old one, no way
   for a caller to deliberately pin an exact version for reproducibility, and
   no way to say "always run whatever's current" either — there's no live
   concept of "current" for something that's just sitting in a directory.

Skillward's answer to each of these is mechanical, not aspirational —
they're direct consequences of a skill never being a local file:

| Local install problem | How Skillward closes it |
|---|---|
| No visibility | One registry; every published skill and its owner are queryable |
| No integrity guarantee | sha256 checksum, verified by the orchestrator on every fetch, before execution |
| No access control | `public`/`private` + `allowed_accounts`, enforced server-side per request |
| No revocation | Change a skill's visibility or remove it once, centrally — nothing to clean up per machine |
| No audit trail | Invocation telemetry: who ran what, when, success/failure (`/accounts/{id}/invocations`) |
| Version drift | One registry, one current version — every fetch gets what's actually published |
| No skill SDLC | Publish a new version once; the owner moves the registry-side pin (or leaves callers on `"latest"`) — a live, centrally-controlled rollout, not a per-machine sync problem |

This isn't a compliance-checkbox pitch — it's the direct, mechanical answer
to "what's running and who put it there," which local-file skill
distribution structurally has no answer for.

## How this relates to MCP and Agent Skills (and what it doesn't do)

Skillward is a **third, separate mechanism** — not a replacement for, and not
interoperable with, either of the two discovery systems already in the agent
ecosystem. Read this before assuming "skill discovery" is something Skillward
invented, or that it plugs into either of the others automatically.

| | MCP tools (`tools/list`) | Agent Skills (`SKILL.md`) | Skillward |
|---|---|---|---|
| Transport | Network — MCP's JSON-RPC protocol | Local filesystem scan | Network — plain HTTPS, this repo's own manifest format |
| What's discovered | Callable functions exposed by a live server | Procedural instructions + bundled scripts | Small executable skills behind a signed/hashed manifest |
| Auth on discovery | Not standardized by the protocol itself | N/A — already-local files | Every request authenticated by Envoy; private skills invisible to non-allow-listed callers |
| Integrity check on what's discovered | None — the spec says to trust the server | N/A — files are already on your disk | sha256 verified by the orchestrator, before it ever executes anything |
| Cross-server catalog | Official MCP Registry (`registry.modelcontextprotocol.io`) | N/A | This repo's own registry — **not** federated with the official one |

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

## Multi-language, plain-text, and chained skills

Two code runtimes ship today: `python3.1x` and `node20` — a skill's
`entrypoint` is `payload.py:function` or `payload.js:function`, dispatched
by its declared `runtime`. Adding a third language means adding a bootstrap
script and a dispatch branch in `skillward/sandbox.py`; nothing in the
orchestrator or protocol needs to change.

A skill doesn't have to be executable code at all. The `text` runtime covers
skills that are just static content — a prompt, a set of instructions,
reference material, in the spirit of a SKILL.md — where `entrypoint` is a
bare `skill.md` or `notes.txt` with no function to call, and invoking the
skill just returns `{"text": "<payload contents>"}`. No subprocess, no
capability surface, nothing to sandbox — but it still goes through the same
discover→authenticate→authorize→fetch→verify pipeline, checksummed and
access-controlled exactly like a code skill:

```python
result = orchestrator.invoke("greeting-prompt", "1.0.0", {})
# {"text": "You are a friendly assistant. Always greet the user warmly."}
```

A catalog of reusable prompts benefits from the same integrity and
access-control story as a catalog of code — that's not a special case
Skillward carves out for text, it's the same protocol applied to a different
kind of payload.

A skill also doesn't have to be *only* one or the other. Publish with
`files` instead of `code` to bundle a script together with a companion
text file — the shape most "typical" skills outside this protocol actually
take: a `SKILL.md`-style instructions file plus one or more scripts,
versioned together as one unit:

```python
httpx.post(
    f"{GATEWAY_URL}/skills/doubling-assistant/1.0.0",
    headers=auth,
    json={
        "name": "Doubling Assistant",
        "runtime": "python3.13",
        "entrypoint": "run.py:run",
        "input_schema": {...},
        "output_schema": {...},
        "files": {
            "run.py": "def run(input_data):\n    return {'greeting': __bundle__['SKILL.md'], 'n': input_data['n'] * 2}\n",
            "SKILL.md": "You are a doubling assistant.",
        },
    },
)

orchestrator.invoke("doubling-assistant", "1.0.0", {"n": 21})
# {"greeting": "You are a doubling assistant.", "n": 42}
```

`__bundle__` is a plain in-memory mapping of every file published alongside
the entrypoint (path → content) — not real filesystem access, so a companion
file is readable without granting anything like an `fs:` capability just to
see what was published with it. The whole bundle is hashed and verified as
one unit, and an ordinary single-file skill is unaffected — it's just
treated as a one-entry bundle internally, nothing about publishing or
invoking it changes.

A skill can hand off to another skill by *returning* a reserved shape instead
of a real result — there's no live callback, no long-running process, and no
runtime-specific protocol, so this works the same for every runtime:

```python
def run(input_data):
    return {"call_next": {"id": "some-other-skill", "version": "1.0.0", "input": {"n": input_data["n"] + 1}}}
```

A skill can only tail-call this way — it can't get the next skill's result
back and keep computing on it in the same run, since its own process has
already returned and exited by the time the next hop starts. A chain that
needs to combine results expresses that as more single-purpose skills, each
handing off with everything the next one needs already in `input`. Every hop
after the first also gets a reserved `_chain_context` input key: the ordered
output of every prior hop in the chain, not just whatever the immediately
previous hop chose to forward, so a hop can see further back than one step
without every skill in between having to thread that data through by hand.

Following a hand-off is gated the same way as any other capability: the
*calling* skill must declare `skill:<id>` (or `skill:*`), and the deployment
must separately grant it. The orchestrator's own main loop — not the skill's
code — decides whether to follow the hand-off, then runs the *full* protocol
again for the target: discovery, authorization, its own checksum
verification, its own sandbox. It's capped at a max chain depth
(`SkillwardOrchestrator.MAX_CHAIN_DEPTH`, 5 by default) and bounded by one
shared deadline across the whole chain, so a cycle or a runaway or slow chain
can't recurse or stall forever. The sandboxed subprocess never gets raw
network access to reach the registry itself — a skill can only ask for a
hand-off, never perform one. See spec/SPEC.md's "Chain calls" section for the
full design.

## Network access

A skill granted `net:<url-glob>` capabilities gets `__net_fetch__` in its
execution namespace — a real HTTP client, checked against exactly the
patterns it was granted before any request goes out:

```python
def run(input_data):
    resp = __net_fetch__("https://api.example.com/rates", method="GET")
    return {"status": resp["status"], "body": resp["body"]}
```

```js
async function run(input) {
  const resp = await __net_fetch__("https://api.example.com/rates");
  return { status: resp.status, body: resp.body };
}
```

Declare it like any other capability — `"capabilities": ["net:https://api.example.com/*"]`
on the manifest, granted by the deployment's `allowed_capabilities` — and a
request to a URL that doesn't match is rejected by `__net_fetch__` itself,
inside the sandbox, before anything goes out. A skill granted no `net:`
capability at all has no such function available to call. See the security
model below for what this boundary actually guarantees per runtime.

The result carries both `body` (best-effort utf-8 text) and `body_base64`
(the exact response bytes) — fetch something binary, like a PDF, and `body`
will be corrupted by the utf-8 decode; use `body_base64` and decode it
yourself instead.

### Worked example: find PDFs on a page, then summarize them

`examples/find_and_summarize_pdfs.py` puts `__net_fetch__` and the
skill/orchestrator split to work end-to-end, against a real website: one
skill fetches a page and returns every PDF link on it — that's the *only*
thing that runs inside the sandbox. Picking the most relevant links,
downloading the actual PDFs, extracting their text, and summarizing each
with an LLM all happen afterward, in the orchestrator's own trusted code,
never inside the sandbox. That split is deliberate: an LLM API key and raw
PDF bytes should never have to enter untrusted skill code.

```bash
brew install poppler                  # provides pdftotext, used for text extraction
export OPENROUTER_API_KEY=sk-or-...   # https://openrouter.ai/keys — optional, omit to just list PDFs

python -m examples.find_and_summarize_pdfs https://example.com/reports/
```

The skill is granted `net:` access to exactly the host in the URL you pass
— nothing broader, and nothing standing between runs. Point it at any page
that links to PDFs (a company filings page, a government reports index, an
academic publications list) to see it work against something of your own.

### Worked example: the same pipeline as a two-hop chain

`examples/chain_fetch_and_summarize_pdf.py` expresses that same "fetch, then
summarize" pipeline entirely as two chained skills instead — `pdf-fetcher`
downloads a PDF (using `body_base64` for byte-exact binary fidelity — `body`
alone would corrupt it), extracts its text with a small dependency-free
extractor, and hands off to `pdf-summarizer` via `call_next`; `pdf-summarizer`
calls Gemini through OpenRouter using an `env:`-granted API key and returns
the summary as the final result. The orchestrator follows the hand-off
between them itself — no glue code runs in between.

```bash
export OPENROUTER_API_KEY=sk-or-...   # https://openrouter.ai/keys — required this time
python -m examples.chain_fetch_and_summarize_pdf https://example.com/report.pdf
```

Worth being explicit about the trade-off this makes versus the single-skill
version above: putting the whole pipeline inside skills means the OpenRouter
API key and the PDF's raw bytes both have to enter sandboxed skill code,
which the single-skill version deliberately avoids. Reach for that one by
default; this one exists to show a real two-hop `call_next` chain doing
substantive work at each step, not just passing a value through.

## Choosing a skill version

Every call names a version, but only one of the three ways to do that is a
caller decision:

```python
orchestrator.invoke("some-skill", "1.2.0", input_data)  # exact override, for reproducibility
orchestrator.invoke("some-skill", "latest", input_data)  # always the newest published version
orchestrator.invoke("some-skill", "pinned", input_data)  # whatever the registry currently has pinned
orchestrator.list_versions("some-skill")  # ["1.3.0", "1.2.0", "1.0.0"], newest first
```

**Pinning is a registry decision, not an orchestrator one.** `"pinned"` is
the one to reach for by default — which version it resolves to is set by
the skill's owner, centrally, not hardcoded into every call site:

```python
orchestrator.set_pin("some-skill", "1.2.0")  # owner only — "pinned" now means 1.2.0
orchestrator.get_pin("some-skill")  # "1.2.0"
orchestrator.clear_pin("some-skill")  # unpin — "pinned" now behaves like "latest"
```

This is the piece a local skills directory has no equivalent for: there's
no live "current" to ask for on a filesystem, and no way to roll every
caller onto a new version except updating each machine individually. Here,
the skill's owner moves the pin once — via the registry, not by pushing a
change to every agent that calls the skill — and every caller using
`"pinned"` picks it up on its next call.

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
- **`net:` capabilities are pattern-checked, not ambient.** A skill granted
  `net:<url-glob>` gets `__net_fetch__(url, ...)`, which checks `url` against
  exactly the granted patterns before making a real request — a skill
  granted nothing gets no such function. For Node this is a hard boundary
  (the vm context it runs in starts with nothing else in it — no `require`,
  no global `fetch`); for Python it's best-effort, since stdlib access isn't
  removed and code that imports `urllib` directly bypasses the check. See
  spec/SPEC.md's "5b. Network access."

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

**Known limitation, stated plainly**: this is not, and is not trying to be, a
replacement for MCP or Agent Skills — see "How this relates to MCP and Agent
Skills" above. It has no federation with the official MCP Registry, no
mechanism for an MCP server to expose a Skillward skill (or vice versa), and
no way for an agent's Skills directory to pick up a Skillward skill without
an adapter doing that translation explicitly. Skillward-discovered skills are
a fourth kind of "thing an agent can do," alongside — not instead of — MCP
tools, Agent Skills, and whatever a given framework already calls a "tool."
