"""Full-stack tests against the shared stack from conftest.py (registry +
partner_service + labs_service + Envoy, on dedicated test ports).

Run with: pytest tests/ -v (from the project root, inside .venv)
"""

import copy

import httpx
import jsonschema
import pytest
from conftest import GATEWAY_URL

from skillward import SkillwardOrchestrator
from skillward.orchestrator import CapabilityDeniedError
from skillward.registry_client import ChecksumMismatchError
from skillward.sandbox import SkillExecutionError


def test_unauthenticated_requests_are_rejected():
    resp = httpx.get(f"{GATEWAY_URL}/discover")
    assert resp.status_code == 401


def test_internal_authz_not_externally_routable():
    resp = httpx.get(f"{GATEWAY_URL}/internal/authz")
    assert resp.status_code == 404


def test_discover_lists_example_skills(orchestrator):
    ids = {s.id for s in orchestrator.discover()}
    assert {"example-echo", "example-word-count", "partner-currency-convert", "labs-reverse-text"} <= ids


def test_invoke_echo(orchestrator):
    result = orchestrator.invoke("example-echo", "1.0.0", {"message": "hi"})
    assert result == {"echo": "hi", "length": 2}


def test_invoke_word_count(orchestrator):
    result = orchestrator.invoke("example-word-count", "1.0.0", {"text": "a b c"})
    assert result == {"word_count": 3, "char_count": 5}


def test_skill_router_partner_backend(orchestrator):
    result = orchestrator.invoke("partner-currency-convert", "1.0.0", {"amount": 100, "pair": "USD_EUR"})
    assert result == {"converted": 92.0, "rate": 0.92}


def test_skill_router_labs_backend(orchestrator):
    result = orchestrator.invoke("labs-reverse-text", "1.0.0", {"text": "skillward"})
    assert result == {"reversed": "drawlliks"}


def test_input_schema_rejects_bad_input(orchestrator):
    with pytest.raises(jsonschema.ValidationError):
        orchestrator.invoke("example-echo", "1.0.0", {"wrong_field": "hi"})


def test_capability_denied_when_not_allowlisted(orchestrator):
    manifest = orchestrator.registry.get_manifest("example-echo", "1.0.0")
    hostile = copy.deepcopy(manifest).model_copy(update={"capabilities": ["net:https://evil.example/*"]})
    with pytest.raises(CapabilityDeniedError):
        orchestrator.invoke_manifest(hostile, {"message": "hi"})


def test_capability_granted_when_allowlisted(account):
    with SkillwardOrchestrator(
        GATEWAY_URL, api_key=account["api_key"], allowed_capabilities={"net:https://evil.example/*"}
    ) as orch:
        manifest = orch.registry.get_manifest("example-echo", "1.0.0")
        allowed = manifest.model_copy(update={"capabilities": ["net:https://evil.example/*"]})
        result = orch.invoke_manifest(allowed, {"message": "hi"})
        assert result == {"echo": "hi", "length": 2}


def test_tampered_payload_is_rejected(orchestrator, monkeypatch):
    manifest = orchestrator.registry.get_manifest("example-echo", "1.0.0")
    real_get = orchestrator.registry._client.get

    def tampered_get(url, *args, **kwargs):
        resp = real_get(url, *args, **kwargs)
        if url.endswith("/payload"):
            resp._content = resp.content.replace(b"echo", b"pwned")
        return resp

    monkeypatch.setattr(orchestrator.registry._client, "get", tampered_get)
    with pytest.raises(ChecksumMismatchError):
        orchestrator.invoke_manifest(manifest, {"message": "hi"})


def test_skill_exception_surfaces_as_execution_error(orchestrator):
    manifest = orchestrator.registry.get_manifest("example-echo", "1.0.0")
    broken_schema_manifest = manifest.model_copy(update={"input_schema": {"type": "object"}})
    with pytest.raises(SkillExecutionError):
        orchestrator.invoke_manifest(broken_schema_manifest, {"not_message": "hi"})


def test_private_skill_hidden_from_other_accounts(orchestrator, account):
    alice = account
    bob = httpx.post(f"{GATEWAY_URL}/accounts", json={"name": "bob-pytest"}).json()

    publish_resp = httpx.post(
        f"{GATEWAY_URL}/skills/pytest-secret/1.0.0",
        headers={"Authorization": f"Bearer {alice['api_key']}"},
        json={
            "name": "Pytest Secret",
            "description": "private skill for the ACL test",
            "runtime": "python3.13",
            "entrypoint": "payload.py:run",
            "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]},
            "output_schema": {"type": "object", "properties": {"y": {"type": "integer"}}, "required": ["y"]},
            "visibility": "private",
            "code": "def run(input_data):\n    return {'y': input_data['x'] + 1}\n",
        },
    )
    assert publish_resp.status_code == 200

    alice_ids = {
        s["id"]
        for s in httpx.get(
            f"{GATEWAY_URL}/discover", headers={"Authorization": f"Bearer {alice['api_key']}"}
        ).json()
    }
    bob_ids = {
        s["id"]
        for s in httpx.get(f"{GATEWAY_URL}/discover", headers={"Authorization": f"Bearer {bob['api_key']}"}).json()
    }
    assert "pytest-secret" in alice_ids
    assert "pytest-secret" not in bob_ids

    bob_manifest_resp = httpx.get(
        f"{GATEWAY_URL}/skills/pytest-secret/1.0.0/manifest", headers={"Authorization": f"Bearer {bob['api_key']}"}
    )
    assert bob_manifest_resp.status_code == 404


def test_invocation_telemetry_recorded(orchestrator, account):
    # Telemetry is attributed to the skill's *owner*, not just the caller, so
    # this needs a skill the test account actually owns to check its log.
    publish_resp = httpx.post(
        f"{GATEWAY_URL}/skills/pytest-telemetry/1.0.0",
        headers={"Authorization": f"Bearer {account['api_key']}"},
        json={
            "name": "Pytest Telemetry",
            "description": "owned skill for the telemetry test",
            "runtime": "python3.13",
            "entrypoint": "payload.py:run",
            "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]},
            "output_schema": {"type": "object", "properties": {"y": {"type": "integer"}}, "required": ["y"]},
            "visibility": "public",
            "code": "def run(input_data):\n    return {'y': input_data['x']}\n",
        },
    )
    assert publish_resp.status_code == 200

    orchestrator.invoke("pytest-telemetry", "1.0.0", {"x": 1})

    rows = httpx.get(
        f"{GATEWAY_URL}/accounts/{account['account_id']}/invocations",
        headers={"Authorization": f"Bearer {account['api_key']}"},
    ).json()
    assert any(r["skill_id"] == "pytest-telemetry" and r["success"] for r in rows)
