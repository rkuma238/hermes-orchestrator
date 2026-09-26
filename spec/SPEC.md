# Skillward Protocol — v0.1 (draft)

Skillward is a small, transport-agnostic protocol for **discovering, fetching, verifying,
and executing** remote "skills" — small, self-contained pieces of code an
orchestrator (an agent runtime, e.g. a LangChain agent) can pull in on demand
instead of statically installing every possible tool ahead of time.

It intentionally borrows the shape of existing open ecosystems (npm/PyPI package
metadata, OCI image manifests, Sigstore-style signing) rather than inventing new
primitives. Everything a skill needs is declared up front in a **manifest**; the
manifest is public and inspectable even when the payload it points to requires a
license to fetch (out of scope for v0.1 — see "Non-goals").

## Design principles

1. **Every call is authenticated, including discovery.** v0.1 required no
   credential to browse the catalog; as of v0.2 every request — `/discover`
   included — must carry a valid account credential. An unauthenticated
   caller gets rejected before it reaches the registry at all (see
   "Authentication & authorization" below).
2. **Authentication and authorization are different layers.** *Authentication*
   ("is this a request from a known account?") is enforced at the network
   edge, in Envoy, via its `ext_authz` filter — the registry application code
   never sees a request Envoy hasn't already validated. *Authorization*
   ("which skills can this specific account see/run?") is a business-logic
   decision — which skills are private, who they're shared with — and is
   applied by the registry using the account identity Envoy attaches to the
   request. Putting the identity check at the edge and the visibility
   decision in the app is the standard split: an edge proxy is good at
   "reject or forward," not at reshaping a JSON response body per caller.
3. **Deny by default, at two levels.** A skill gets no filesystem, network,
   or environment access unless it's declared in `capabilities`, and the
   orchestrator decides whether to grant each declared capability at run
   time. Independently, a skill is invisible to any account that isn't its
   owner or on its `allowed_accounts` list, unless it's marked `public`.
4. **Integrity over obscurity.** Payloads are verified by checksum (and
   optionally signature) before execution. Access control decides *who* can
   reach a manifest at all; once a caller is authorized, hiding the payload's
   bytes is not itself treated as a security control — verification and
   sandboxing are.
5. **Runtime-agnostic manifest, pluggable execution backend.** v0.1 ships a
   Python subprocess sandbox for a working reference implementation; the
   manifest's `runtime` field leaves room for a WASI/Wasm-component backend
   later without changing the protocol.

## Actors

- **Gateway (Envoy)** — the only network-reachable entry point. Terminates
  every request, calls out to the authz check on each one, and only forwards
  requests that carry a valid account credential — with the account's
  identity attached as a trusted header — to the registry. Reference config:
  `envoy/envoy.yaml`.
- **Registry** — an HTTP service that stores accounts, skill manifests, and
  (optionally proxies or points to) payload bytes; applies per-skill
  authorization on top of the identity the gateway attaches. Reference
  implementation: `registry_server/`.
- **Account** — a registered publisher and/or consumer identity: an `id` and
  an API key. The same account type publishes skills and calls discovery —
  there's no separate "buyer" credential in v0.1.
- **Orchestrator** — the agent runtime that authenticates, discovers,
  fetches, verifies, and executes skills. Reference implementation:
  `skillward/`.

## Protocol flow

```
Orchestrator                    Envoy (gateway)                  Registry
     |  0. Authorization: Bearer <api_key>  on every request below   |
     |------------------------------->|                              |
     |                                 | ext_authz check (per call)  |
     |                                 |----------------------------->|
     |                                 |  200 + x-account-id  /  401  |
     |                                 |<-----------------------------|
     |  1. GET /discover?q=...        |  (forwarded only if 200)     |
     |-------------------------------->|----------------------------->|
     |                                 |    [{authorized manifests}]  |
     |<--------------------------------|<-----------------------------|
     |  2. GET /skills/{id}/{v}/manifest (same auth+authz gate)       |
     |  3. GET {manifest.payload.url}                                |
  4. verify sha256(payload) == manifest.payload.sha256
     (and Ed25519 signature if publisher.public_key set)
  5. check manifest.capabilities against local policy;
     abort if the skill wants something not allowed
  6. instantiate sandbox, run entrypoint(input) -> output,
     validated against input_schema / output_schema
  7. tear down sandbox; best-effort POST invocation record
```

### 0. Authentication (Envoy, every request)

Every request to the gateway (except `POST /accounts`, the signup endpoint)
must carry `Authorization: Bearer <api_key>`. Envoy's `ext_authz` HTTP filter
calls the registry's internal `/internal/authz` endpoint — not routable from
outside — on every incoming request before it reaches any real route. That
endpoint validates the key against the accounts store and returns either
`200` plus an `x-account-id` header (which Envoy copies onto the request it
forwards upstream) or `401`. The registry application code trusts
`x-account-id` completely, because by the time a request reaches it, Envoy
has already guaranteed the header reflects a validated key — the registry
itself is not reachable except through the gateway.

### 1. Discovery (authenticated + authorized)

`GET /discover?q=<free text>&capability=<cap>`

Requires authentication (step 0). Returns an array of manifest *summaries*
(id, version, name, description, capabilities, input/output schema) for
**only the skills `x-account-id` is authorized to see**: every `public`
skill, plus any `private` skill it owns or is on the `allowed_accounts` list
of. A skill that exists but isn't authorized for this caller is simply
omitted — the registry doesn't distinguish "doesn't exist" from "exists but
you can't see it," so discovery doesn't leak the existence of private skills
to accounts outside their ACL.

`q` matches against the skill's id, name, description, and declared
capabilities — not just name/description — so a targeted query (e.g. a
capability string, or a word that only appears in the id) narrows the result
down to the relevant skill(s) instead of forcing the caller to fetch the
whole catalog and reason over it itself. This matters for token/context
budget as much as for convenience: an orchestrator asking a specific
question gets back a short, relevant list, not everything the registry
knows about.

The reference registry caches manifests and payloads in memory after first
load, invalidated on publish — `/discover` and skill fetches don't re-scan
disk on every call. This is a performance property of the reference
implementation, not a protocol requirement, but any registry serving a
non-trivial catalog should do something equivalent: discovery that gets
slower as the catalog grows undermines the whole point of asking a targeted
question instead of listing everything.

### 2. Manifest fetch

`GET /skills/{id}/{version}/manifest` returns the full manifest
(`spec/skill-manifest.schema.json`) if `x-account-id` is authorized for that
skill (owner, allow-listed, or the skill is `public`) — `404`, not `403`,
otherwise, again to avoid confirming a private skill's existence. The
manifest is the unit of trust: it carries the payload URL, its checksum,
declared capabilities, and I/O schema.

`{version}` may be an exact semver, or the literal string `latest` — resolved
centrally by the registry to the highest published semver for that `id`, at
call time. The returned manifest's own `version` field is always the
concrete version that was resolved, never the literal string `latest`.
`GET /skills/{id}/versions` lists every version of `id` this caller is
authorized to see, newest first, for a caller that wants to choose a version
explicitly rather than always taking whatever's current.

This is a version-selection story a local skills directory has no equivalent
for: a file on disk is just whatever happened to be checked out there, with
no live relationship to what's actually current, and no way to ask for "the
newest" without some separate process pulling updates onto that machine.
Centralizing skills behind a registry turns "which version do I run" into a
per-call choice — pin an exact version for reproducibility, or take `latest`
to always get whatever's currently published — resolved the same way for
every caller, rather than however each machine happens to be maintained.

### 3. Payload fetch

`GET {manifest.payload.url}` — today, in the reference implementation, a raw
Python file, served by the same registry and subject to the same
authorization check as the manifest. The registry and payload host may be
different services in a larger deployment (e.g. a CDN) — Skillward doesn't require
them to be the same origin, but whatever serves the payload must apply the
same visibility/ACL check the registry does.

### Publishing

`POST /skills/{id}/{version}` (authenticated) lets an account publish a
skill. The body carries manifest fields plus the raw source; **the registry,
not the publisher, computes `payload.sha256`** over the bytes it actually
stores, so the checksum can't be spoofed at publish time. The publishing
account becomes the skill's owner and can set `visibility` (`public` or
`private`) and `allowed_accounts`.

**A published `(id, version)`'s code is immutable.** Republishing the same
version with different content is rejected (409) — the registry compares
the new digest against the existing one and refuses a mismatch, requiring a
new version instead. Republishing byte-identical content, or changing only
`visibility`/`allowed_accounts`/`capabilities`/`description` while the code
stays the same, is allowed (a no-op on the digest). This guarantee is what
makes it safe for a client to cache verified payload bytes by digest
indefinitely — see "Client-side payload caching" below — without it, a
cached digest could silently stop meaning the same bytes.

### Client-side payload caching (recommended, not required)

Because a digest is guaranteed immutable once published, a payload fetched
and verified for a given `sha256` never needs fetching or re-verifying
again for that same digest — a cache hit is bytes that already passed the
check in "4. Verification" below, by construction. The reference client
caches by digest rather than by `(id, version)`, unbounded for v0.1. This
matters most for chain calls (see "5a." below): a skill called repeatedly,
directly or as a shared dependency inside one call tree, is fetched over
the network exactly once — the same benefit a local skills directory gets
for free just from being files already on disk. The manifest itself is
*not* cached this way — `visibility`/`capabilities`/`allowed_accounts` can
legitimately change on a version even though its code can't, so a manifest
lookup always goes to the registry (which has its own, separate cache —
see "Discovery" above).

### 4. Verification (mandatory)

The orchestrator MUST compute `sha256` over the exact bytes received and
compare to `manifest.payload.sha256`. Mismatch → reject, never execute. If
`manifest.publisher.public_key` is present, the orchestrator SHOULD also
verify `manifest.payload.signature` and MAY refuse unsigned skills under a
stricter policy.

### 5. Capability check

`capabilities` is an allow-list the *skill* declares it needs (e.g.
`net:https://api.example.com/*`). The *orchestrator* independently decides,
per-deployment, which of those it's willing to grant — a manifest asking for
`net:*` doesn't mean the orchestrator has to grant it. Nothing not listed is
ever available to the running skill.

### 5a. Chain calls

A skill hands off to another skill by *returning* a reserved shape instead of
a real result — there is no live callback and no skill process stays running
while the next one executes:

```json
{"call_next": {"id": "next-skill", "version": "1.0.0", "input": {"...": "..."}}}
```

A result is treated as a hand-off only if `call_next` is its *only* top-level
key; anything else is validated against `output_schema` and returned as the
real, final result. This means a skill can never get a chained skill's result
back and keep computing on it in the same run — it can only tail-call. A
chain that needs to combine an earlier result with more computation expresses
that as separate skills: one skill hands off with everything the next one
needs already folded into `input`, and/or reads what earlier hops returned
from `_chain_context` (below).

The orchestrator, not the skill's own code, decides whether a hand-off is
followed. A skill's `capabilities` list can include `skill:<id>` (permission
to hand off to one specific other skill) or `skill:*` (any skill) — checked
the same way as every other capability: declared by the *calling* skill's own
manifest *and* separately granted by the orchestrator's deployment policy.
Neither alone is enough, same as `net:`/`env:`. The orchestrator checks this,
fetches and verifies the target's manifest and payload — the full
discover→authenticate→authorize→fetch→verify→execute pipeline again, not a
shortcut — and runs it as the next hop, all from its own main loop. No
sandboxed subprocess ever gets raw network access to reach the registry
itself; a skill cannot self-service a hand-off, it can only ask for one.

Every hop after the first receives `_chain_context`: a reserved input key,
injected after the hop's own `input_schema` validation (so declaring it isn't
required), holding the ordered list of `{"id", "version", "output"}` for every
prior hop in this chain — not just whatever the immediately-previous hop
chose to forward via `call_next`'s `input`. This lets a hop make decisions
based on earlier results without every intermediate hop having to thread them
through by hand.

One shared wall-clock deadline governs an entire chain, computed once from
the *first* skill's own `resource_limits.timeout_seconds` — a later hop's own
`resource_limits` can only shrink its remaining share of that budget further,
never extend the chain past the original deadline. This is why the loop lives
in the orchestrator rather than inside a long-lived sandboxed process: one
place enforces one deadline across every hop, instead of each hop getting an
independent timeout that a long enough chain could exceed in aggregate.

Two backstops against runaway chains: a max chain depth
(`SkillwardOrchestrator.MAX_CHAIN_DEPTH`, 5 by default) enforced by the
orchestrator between every hop, and the fact that each hop still goes through
the same capability/authorization checks as a top-level call — a compromised
or buggy skill can't use chaining to reach something it couldn't have called
directly. Both raise directly as `ChainDepthExceededError` /
`CapabilityDeniedError` from the orchestrator's own loop — there's no
subprocess boundary to cross between hops, so nothing needs to be degraded to
a generic wrapped error to get the failure back to the caller.

This works identically for every runtime (`python3.1x`, `node20`, ...): the
sandbox backend (`skillward/sandbox.py`) never interprets `call_next` at
all — it just returns whatever the skill returned, and the orchestrator
decides what it means. A hand-off from a Python skill to a Node skill (or
vice versa) needs no special handling anywhere.

### 6. Execution

For code runtimes (`python3.1x`, `node20`), the orchestrator loads
`entrypoint` (`file.py:function` or `file.js:function`, dispatched by
`manifest.runtime`) inside a sandbox (see `skillward/sandbox.py`), calls it
with a JSON-serializable dict validated against `input_schema`, and validates
the returned dict against `output_schema`.

The `text` runtime isn't code at all — `entrypoint` is a bare `file.md` or
`file.txt` with no function to call, and the payload's own bytes *are* the
result (`{"text": "<payload contents>"}`), still validated against
`output_schema` like any other result. There's no subprocess, no capability
surface, nothing to sandbox — a text skill is static content (a prompt,
instructions, reference material, in the spirit of a SKILL.md) served
through the same discover→authenticate→authorize→fetch→verify pipeline as
any code skill, not a special case of it. This is deliberately *not* limited
to binary/executable skills: a catalog of reusable prompts or instructions
benefits from the same integrity and access-control story as a catalog of
code.

See "Sandboxing" below for what isolation actually means for code runtimes in
the reference implementation vs. what a
production deployment should use.

### 7. Teardown

The sandbox process/instance is discarded after the call. No skill payload or
scratch state persists on the orchestrator host between invocations. This is
about statelessness and per-call isolation between tenants/skills — not about
concealment.

## Sandboxing (be honest about what this buys you)

The v0.1 reference sandbox (`skillward/sandbox.py::SubprocessSandboxRunner`) runs
each invocation in a fresh `python -I -S` subprocess, with a scrubbed
environment, a throwaway temp `cwd`, a wall-clock timeout, and an RLIMIT on
memory/CPU. That stops *accidental* misbehavior and gives you process-level
crash isolation, but **CPython subprocess isolation is not a hard security
boundary against a deliberately malicious skill** — there's no seccomp/network
namespace here, and a determined payload can still make network calls in the
absence of OS-level enforcement.

Treat v0.1 as suitable for skills you trust (your own team, vetted publishers)
during development. Before running arbitrary third-party skills in production,
swap in a `SandboxRunner` backed by one of:

- A container run with `--network=none --read-only` and dropped capabilities,
  or gVisor/Firecracker for kernel-level isolation.
- A WebAssembly Component Model runtime (e.g. Wasmtime) — true capability-based
  sandboxing with no ambient authority, and the natural next step once skills
  are compiled rather than interpreted.

The `SandboxRunner` interface is deliberately small (`run(entrypoint, input,
capabilities, limits) -> output`) so this swap doesn't touch the rest of the
orchestrator.

## Non-goals for v0.1

- **Payment/licensing.** Out of scope for now. The manifest's `payload.url`
  can point at a licensed/gated endpoint later without changing this spec —
  that's a registry-side concern, not a protocol concern.
- **Signature *requirement*.** Signing is supported but optional in v0.1;
  policy on whether to require it is left to the orchestrator deployment.
- **Multi-file skill bundles.** Each skill's implementation is still a
  single file (`payload.py` or `payload.js`) — no bundling multiple source
  files or dependencies for one skill yet.
