"""One skill handing off to another via the reserved call_next output shape,
against the real registry + gateway stack from conftest.py — not just the
direct sandbox-level checks already covered elsewhere. Publishes its own
skills so the hand-off capability requirements are exercised for real,
including denial and the chain-depth backstop.

A skill can't get a chained skill's result back and keep computing (there's
no live callback — see orchestrator.py's module docstring for why); it can
only tail-call: return exactly {"call_next": {"id", "version", "input"}}
instead of a real result, and the orchestrator picks up from there between
hops. Anything a hop needs from earlier in the chain either has to be
threaded through explicitly via call_next's "input", or read from the
reserved "_chain_context" key the orchestrator injects into every hop after
the first (see test_chain_context_carries_prior_outputs below).
"""

import json

import httpx
import pytest
from conftest import GATEWAY_URL

from skillward import SkillwardOrchestrator
from skillward.orchestrator import CapabilityDeniedError, ChainDepthExceededError
from skillward.registry_client import ChecksumMismatchError

_DEFAULT_SCHEMA = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}


def _publish(
    api_key: str,
    skill_id: str,
    version: str,
    *,
    capabilities=None,
    code: str,
    description: str,
    runtime: str = "python3.13",
    entrypoint: str = "payload.py:run",
    input_schema: dict | None = None,
    output_schema: dict | None = None,
):
    resp = httpx.post(
        f"{GATEWAY_URL}/skills/{skill_id}/{version}",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "name": skill_id,
            "description": description,
            "runtime": runtime,
            "entrypoint": entrypoint,
            "input_schema": input_schema or _DEFAULT_SCHEMA,
            "output_schema": output_schema or _DEFAULT_SCHEMA,
            "capabilities": capabilities or [],
            "visibility": "public",
            "code": code,
        },
    )
    resp.raise_for_status()
    return resp.json()


INCREMENTER_CODE = "def run(input_data):\n    return {'n': input_data['n'] + 1}\n"

DOUBLER_CODE = "def run(input_data):\n    return {'n': input_data['n'] * 2}\n"

# Hands off to chain-doubler instead of computing anything itself — the
# increment-then-double result (10 + 1) * 2 == 22 now comes from two single-
# purpose skills each doing one thing, rather than one skill calling another
# and combining the result, which the trampoline model doesn't support.
ADDER_CODE = (
    "def run(input_data):\n"
    "    return {'call_next': {'id': 'chain-doubler', 'version': '1.0.0', 'input': {'n': input_data['n'] + 1}}}\n"
)

SELF_CALLER_CODE = (
    "def run(input_data):\n"
    "    return {'call_next': {'id': 'chain-self-caller', 'version': '1.0.0', 'input': {'n': input_data['n'] + 1}}}\n"
)


@pytest.fixture(scope="module")
def chain_account():
    # Module-scoped and shared: several tests below republish the same
    # skill ids unchanged, which the registry only allows when the same
    # account already owns them (see publish_skill's ownership check) — a
    # fresh account per test would 403 on the second publish.
    resp = httpx.post(f"{GATEWAY_URL}/accounts", json={"name": "chain-call-tests"})
    resp.raise_for_status()
    return resp.json()


def test_chain_call_succeeds_when_capability_granted(chain_account):
    api_key = chain_account["api_key"]
    _publish(api_key, "chain-doubler", "1.0.0", code=DOUBLER_CODE, description="doubles n")
    _publish(
        api_key,
        "chain-adder",
        "1.0.0",
        capabilities=["skill:chain-doubler"],
        code=ADDER_CODE,
        description="increments n, hands off to chain-doubler",
    )

    with SkillwardOrchestrator(GATEWAY_URL, api_key=api_key, allowed_capabilities={"skill:chain-doubler"}) as orch:
        result = orch.invoke("chain-adder", "1.0.0", {"n": 10})
    assert result == {"n": 22}  # (10 + 1) * 2


def test_chain_call_denied_when_not_declared_on_calling_skill(chain_account):
    api_key = chain_account["api_key"]
    _publish(api_key, "chain-doubler", "1.0.0", code=DOUBLER_CODE, description="doubles n")
    # No skill: capability declared this time — the hand-off itself runs
    # fine (nothing about running chain-adder needs a capability), but the
    # orchestrator refuses to follow the call_next it returns.
    _publish(
        api_key,
        "chain-adder-undeclared",
        "1.0.0",
        capabilities=[],
        code=ADDER_CODE,
        description="tries to hand off to chain-doubler without declaring it",
    )

    with SkillwardOrchestrator(GATEWAY_URL, api_key=api_key, allowed_capabilities=set()) as orch:
        with pytest.raises(CapabilityDeniedError, match="not permitted to hand off"):
            orch.invoke("chain-adder-undeclared", "1.0.0", {"n": 10})


def test_chain_call_denied_when_deployment_policy_refuses_it(chain_account):
    api_key = chain_account["api_key"]
    _publish(api_key, "chain-doubler", "1.0.0", code=DOUBLER_CODE, description="doubles n")
    _publish(
        api_key,
        "chain-adder-2",
        "1.0.0",
        capabilities=["skill:chain-doubler"],
        code=ADDER_CODE.replace("chain-adder", "chain-adder-2"),
        description="declares the capability but the deployment won't grant it",
    )

    # Deliberately empty allowed_capabilities: the *skill* declares
    # skill:chain-doubler, but this deployment refuses to grant it — same as
    # any other capability prefix, checked before the skill even runs.
    with SkillwardOrchestrator(GATEWAY_URL, api_key=api_key, allowed_capabilities=set()) as orch:
        with pytest.raises(CapabilityDeniedError):
            orch.invoke("chain-adder-2", "1.0.0", {"n": 10})


def test_chain_call_verifies_checksum_of_chained_skill(chain_account, monkeypatch):
    api_key = chain_account["api_key"]
    _publish(api_key, "chain-doubler", "1.0.0", code=DOUBLER_CODE, description="doubles n")
    _publish(
        api_key,
        "chain-adder-3",
        "1.0.0",
        capabilities=["skill:chain-doubler"],
        code=ADDER_CODE.replace("chain-adder", "chain-adder-3"),
        description="hand-off whose target payload gets tampered with mid-flight",
    )

    with SkillwardOrchestrator(GATEWAY_URL, api_key=api_key, allowed_capabilities={"skill:chain-doubler"}) as orch:
        real_get = orch.registry._client.get

        def tampered_get(url, *args, **kwargs):
            resp = real_get(url, *args, **kwargs)
            if url.endswith("/chain-doubler/1.0.0/payload"):
                resp._content = resp.content.replace(b"* 2", b"* 999")
            return resp

        monkeypatch.setattr(orch.registry._client, "get", tampered_get)

        # The hand-off target's own checksum verification must catch this.
        # It's raised directly by the orchestrator's own fetch — there's no
        # subprocess boundary between hops in this design — so it surfaces
        # as the real ChecksumMismatchError, not wrapped in anything else.
        with pytest.raises(ChecksumMismatchError):
            orch.invoke("chain-adder-3", "1.0.0", {"n": 10})


def test_chain_depth_limit_stops_recursion(chain_account):
    api_key = chain_account["api_key"]
    _publish(
        api_key,
        "chain-self-caller",
        "1.0.0",
        capabilities=["skill:*"],
        code=SELF_CALLER_CODE,
        description="hands off to itself forever unless the depth limit stops it",
    )

    with SkillwardOrchestrator(GATEWAY_URL, api_key=api_key, allowed_capabilities={"skill:*"}) as orch:
        with pytest.raises(ChainDepthExceededError, match="exceeded max depth"):
            orch.invoke("chain-self-caller", "1.0.0", {"n": 0})


def test_chain_call_errors_are_not_wrapped_by_a_sandbox_boundary(chain_account):
    # There's no subprocess running while a hand-off between hops is being
    # decided (the previous hop already returned and exited), so unlike the
    # old interactive design, these errors don't need to cross a sandbox
    # boundary at all — they're real, undegraded exception types, not a
    # generic error wrapping a preserved message string.
    api_key = chain_account["api_key"]
    _publish(
        api_key,
        "chain-self-caller-2",
        "1.0.0",
        capabilities=["skill:*"],
        code=SELF_CALLER_CODE.replace("chain-self-caller", "chain-self-caller-2"),
        description="depth-limit exception-type check",
    )
    with SkillwardOrchestrator(GATEWAY_URL, api_key=api_key, allowed_capabilities={"skill:*"}) as orch:
        with pytest.raises(ChainDepthExceededError) as exc_info:
            orch.invoke("chain-self-caller-2", "1.0.0", {"n": 0})
        assert exc_info.type is ChainDepthExceededError


CONTEXT_FIRST_CODE = (
    "def run(input_data):\n"
    "    return {'call_next': {'id': 'chain-context-second', 'version': '1.0.0', 'input': {'n': input_data['n']}}}\n"
)

CONTEXT_SECOND_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "n": {"type": "integer"},
        "chain_len": {"type": "integer"},
        "prior_id": {"type": "string"},
    },
    "required": ["n", "chain_len", "prior_id"],
}

CONTEXT_SECOND_CODE = (
    "def run(input_data):\n"
    "    ctx = input_data.get('_chain_context', [])\n"
    "    return {\n"
    "        'n': input_data['n'],\n"
    "        'chain_len': len(ctx),\n"
    "        'prior_id': ctx[0]['id'] if ctx else 'none',\n"
    "    }\n"
)


def test_chain_context_carries_prior_outputs(chain_account):
    api_key = chain_account["api_key"]
    _publish(
        api_key,
        "chain-context-second",
        "1.0.0",
        code=CONTEXT_SECOND_CODE,
        description="reads _chain_context to see what ran before it",
        output_schema=CONTEXT_SECOND_OUTPUT_SCHEMA,
    )
    _publish(
        api_key,
        "chain-context-first",
        "1.0.0",
        capabilities=["skill:chain-context-second"],
        code=CONTEXT_FIRST_CODE,
        description="hands off without knowing chain-context-second reads _chain_context",
    )

    with SkillwardOrchestrator(
        GATEWAY_URL, api_key=api_key, allowed_capabilities={"skill:chain-context-second"}
    ) as orch:
        result = orch.invoke("chain-context-first", "1.0.0", {"n": 5})

    # chain-context-first never put anything about itself into the "input"
    # it forwarded — chain-context-second only knows a prior hop existed,
    # and which skill it was, because the orchestrator injected that history
    # itself, not because chain-context-first passed it along explicitly.
    assert result["chain_len"] == 1
    assert result["prior_id"] == "chain-context-first"


NODE_DOUBLER_CODE = "function run(input) { return { n: input.n * 2 }; }"


def test_chain_call_hands_off_across_runtimes(chain_account):
    api_key = chain_account["api_key"]
    _publish(
        api_key,
        "chain-node-doubler",
        "1.0.0",
        code=NODE_DOUBLER_CODE,
        description="doubles n in node20 — the other end of a python hand-off",
        runtime="node20",
        entrypoint="payload.js:run",
    )
    _publish(
        api_key,
        "chain-py-adder",
        "1.0.0",
        capabilities=["skill:chain-node-doubler"],
        code=ADDER_CODE.replace("chain-doubler", "chain-node-doubler").replace("chain-adder", "chain-py-adder"),
        description="python skill handing off to a node skill",
    )

    with SkillwardOrchestrator(
        GATEWAY_URL, api_key=api_key, allowed_capabilities={"skill:chain-node-doubler"}
    ) as orch:
        result = orch.invoke("chain-py-adder", "1.0.0", {"n": 10})
    assert result == {"n": 22}


def test_text_skill_can_hand_off_via_call_next(chain_account):
    # A text skill can't compute a hand-off dynamically — there's no code
    # running, so it can't inspect its own input the way ADDER_CODE does —
    # but it can declare a *fixed* one: if its own content is exactly the
    # reserved call_next JSON shape, the orchestrator follows it exactly
    # like any code skill's hand-off. See sandbox.py's _run_text.
    api_key = chain_account["api_key"]
    _publish(
        api_key,
        "text-chain-target",
        "1.0.0",
        code=DOUBLER_CODE,
        description="the fixed target a text skill hands off to",
    )

    router_content = json.dumps({"call_next": {"id": "text-chain-target", "version": "1.0.0", "input": {"n": 21}}})
    _publish(
        api_key,
        "text-chain-router",
        "1.0.0",
        capabilities=["skill:text-chain-target"],
        code=router_content,
        description="a text skill that always hands off to text-chain-target",
        runtime="text",
        entrypoint="router.json",
        input_schema={"type": "object"},  # a text skill ignores its own input entirely
    )

    with SkillwardOrchestrator(
        GATEWAY_URL, api_key=api_key, allowed_capabilities={"skill:text-chain-target"}
    ) as orch:
        result = orch.invoke("text-chain-router", "1.0.0", {})
    assert result == {"n": 42}


def test_text_skill_hand_off_still_enforces_capability_check(chain_account):
    # Same denial semantics as a code skill's hand-off: declaring
    # skill:<id> is what's checked, not the runtime that declares it.
    api_key = chain_account["api_key"]
    _publish(
        api_key,
        "text-chain-target-2",
        "1.0.0",
        code=DOUBLER_CODE,
        description="a target a text skill tries to reach without permission",
    )

    router_content = json.dumps(
        {"call_next": {"id": "text-chain-target-2", "version": "1.0.0", "input": {"n": 21}}}
    )
    _publish(
        api_key,
        "text-chain-router-undeclared",
        "1.0.0",
        capabilities=[],  # no skill: capability declared this time
        code=router_content,
        description="a text skill that tries to hand off without declaring it",
        runtime="text",
        entrypoint="router.json",
        input_schema={"type": "object"},
    )

    with SkillwardOrchestrator(GATEWAY_URL, api_key=api_key, allowed_capabilities=set()) as orch:
        with pytest.raises(CapabilityDeniedError, match="not permitted to hand off"):
            orch.invoke("text-chain-router-undeclared", "1.0.0", {})
