"""One skill invoking another via call_skill(), against the real registry +
gateway stack from conftest.py — not just the direct sandbox-level checks
already covered elsewhere. Publishes its own skills so the chain-call
capability requirements are exercised for real, including denial and the
chain-depth backstop.
"""

import httpx
import pytest
from conftest import GATEWAY_URL

from skillward import SkillwardOrchestrator
from skillward.orchestrator import CapabilityDeniedError
from skillward.sandbox import SkillExecutionError


def _publish(api_key: str, skill_id: str, version: str, *, capabilities=None, code: str, description: str):
    resp = httpx.post(
        f"{GATEWAY_URL}/skills/{skill_id}/{version}",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "name": skill_id,
            "description": description,
            "runtime": "python3.13",
            "entrypoint": "payload.py:run",
            "input_schema": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
            "output_schema": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
            "capabilities": capabilities or [],
            "visibility": "public",
            "code": code,
        },
    )
    resp.raise_for_status()
    return resp.json()


INCREMENTER_CODE = "def run(input_data):\n    return {'n': input_data['n'] + 1}\n"

DOUBLER_CODE = (
    "def run(input_data):\n"
    "    incremented = call_skill('chain-incrementer', '1.0.0', {'n': input_data['n']})\n"
    "    return {'n': incremented['n'] * 2}\n"
)

SELF_CALLER_CODE = (
    "def run(input_data):\n    return call_skill('chain-self-caller', '1.0.0', {'n': input_data['n'] + 1})\n"
)


@pytest.fixture(scope="module")
def chain_account():
    # Module-scoped and shared: several tests below republish
    # "chain-incrementer"@1.0.0 unchanged, which the registry only allows
    # when the same account already owns it (see publish_skill's ownership
    # check) — a fresh account per test would 403 on the second publish.
    resp = httpx.post(f"{GATEWAY_URL}/accounts", json={"name": "chain-call-tests"})
    resp.raise_for_status()
    return resp.json()


def test_chain_call_succeeds_when_capability_granted(chain_account):
    api_key = chain_account["api_key"]
    _publish(api_key, "chain-incrementer", "1.0.0", code=INCREMENTER_CODE, description="adds 1")
    _publish(
        api_key,
        "chain-doubler",
        "1.0.0",
        capabilities=["skill:chain-incrementer"],
        code=DOUBLER_CODE,
        description="calls chain-incrementer then doubles",
    )

    with SkillwardOrchestrator(
        GATEWAY_URL, api_key=api_key, allowed_capabilities={"skill:chain-incrementer"}
    ) as orch:
        result = orch.invoke("chain-doubler", "1.0.0", {"n": 10})
    assert result == {"n": 22}  # (10 + 1) * 2


def test_chain_call_denied_when_not_declared_on_calling_skill(chain_account):
    api_key = chain_account["api_key"]
    _publish(api_key, "chain-incrementer", "1.0.0", code=INCREMENTER_CODE, description="adds 1")
    # No skill: capability declared this time.
    _publish(
        api_key,
        "chain-doubler-undeclared",
        "1.0.0",
        capabilities=[],
        code=DOUBLER_CODE.replace("chain-incrementer", "chain-incrementer"),
        description="tries to call chain-incrementer without declaring it",
    )

    with SkillwardOrchestrator(GATEWAY_URL, api_key=api_key, allowed_capabilities=set()) as orch:
        with pytest.raises(SkillExecutionError, match="not permitted to call"):
            orch.invoke("chain-doubler-undeclared", "1.0.0", {"n": 10})


def test_chain_call_denied_when_deployment_policy_refuses_it(chain_account):
    api_key = chain_account["api_key"]
    _publish(api_key, "chain-incrementer", "1.0.0", code=INCREMENTER_CODE, description="adds 1")
    _publish(
        api_key,
        "chain-doubler-2",
        "1.0.0",
        capabilities=["skill:chain-incrementer"],
        code=DOUBLER_CODE,
        description="declares the capability but the deployment won't grant it",
    )

    # Deliberately empty allowed_capabilities: the *skill* declares
    # skill:chain-incrementer, but this deployment refuses to grant it —
    # same as any other capability prefix, checked before the skill runs.
    with SkillwardOrchestrator(GATEWAY_URL, api_key=api_key, allowed_capabilities=set()) as orch:
        with pytest.raises(CapabilityDeniedError):
            orch.invoke("chain-doubler-2", "1.0.0", {"n": 10})


def test_chain_call_verifies_checksum_of_chained_skill(chain_account, monkeypatch):
    api_key = chain_account["api_key"]
    _publish(api_key, "chain-incrementer", "1.0.0", code=INCREMENTER_CODE, description="adds 1")
    _publish(
        api_key,
        "chain-doubler-3",
        "1.0.0",
        capabilities=["skill:chain-incrementer"],
        code=DOUBLER_CODE.replace("chain-doubler", "chain-doubler-3").replace(
            "chain-incrementer", "chain-incrementer"
        ),
        description="chain call whose target payload gets tampered with mid-flight",
    )

    with SkillwardOrchestrator(
        GATEWAY_URL, api_key=api_key, allowed_capabilities={"skill:chain-incrementer"}
    ) as orch:
        real_get = orch.registry._client.get

        def tampered_get(url, *args, **kwargs):
            resp = real_get(url, *args, **kwargs)
            if url.endswith("/chain-incrementer/1.0.0/payload"):
                resp._content = resp.content.replace(b"+ 1", b"+ 999")
            return resp

        monkeypatch.setattr(orch.registry._client, "get", tampered_get)

        # The chained call's own checksum verification must catch this —
        # the tampering happens on the *nested* fetch, not the top-level one.
        with pytest.raises(SkillExecutionError):
            orch.invoke("chain-doubler-3", "1.0.0", {"n": 10})


def test_chain_depth_limit_stops_recursion(chain_account):
    api_key = chain_account["api_key"]
    _publish(
        api_key,
        "chain-self-caller",
        "1.0.0",
        capabilities=["skill:*"],
        code=SELF_CALLER_CODE,
        description="calls itself forever unless the depth limit stops it",
    )

    with SkillwardOrchestrator(GATEWAY_URL, api_key=api_key, allowed_capabilities={"skill:*"}) as orch:
        with pytest.raises(SkillExecutionError, match="chain call depth exceeded|max chain depth"):
            orch.invoke("chain-self-caller", "1.0.0", {"n": 0})


def test_chain_call_reports_a_clear_error_for_unauthorized_error_type(chain_account):
    # ChainDepthExceededError and CapabilityDeniedError raised *inside* the
    # on_call_skill callback surface to the calling skill as a generic
    # RuntimeError with the message preserved (the sandbox boundary can't
    # carry Python exception types across the subprocess, only text) — and
    # from there out to the caller as SkillExecutionError. Confirming the
    # message text survives that round trip intact, since it's the only
    # signal available at that point.
    api_key = chain_account["api_key"]
    _publish(
        api_key,
        "chain-self-caller-2",
        "1.0.0",
        capabilities=["skill:*"],
        code=SELF_CALLER_CODE.replace("chain-self-caller", "chain-self-caller-2"),
        description="depth-limit message-preservation check",
    )
    with SkillwardOrchestrator(GATEWAY_URL, api_key=api_key, allowed_capabilities={"skill:*"}) as orch:
        with pytest.raises(SkillExecutionError) as exc_info:
            orch.invoke("chain-self-caller-2", "1.0.0", {"n": 0})
        assert "depth" in str(exc_info.value).lower()
