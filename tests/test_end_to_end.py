"""Full-stack tests: registry + partner_service + labs_service + Envoy, all
spun up as real subprocesses on dedicated test ports (distinct from the dev
ports so this suite can run alongside a manually-running dev stack).

Run with: pytest tests/ -v (from the project root, inside .venv)
"""

import copy
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import jsonschema
import pytest
import yaml

from scripts.generate_envoy_config import build_config
from skillward import SkillwardOrchestrator
from skillward.orchestrator import CapabilityDeniedError
from skillward.registry_client import ChecksumMismatchError
from skillward.sandbox import SkillExecutionError

REPO_ROOT = Path(__file__).parent.parent
GATEWAY_URL = "http://127.0.0.1:18010"

TEST_SPEC = {
    "backends": [
        {
            "name": "registry_service",
            "host": "127.0.0.1",
            "port": 18079,
            "route_prefix": "/",
            "authz_backend": True,
            "public_paths": [
                {"prefix": "/accounts", "method": "POST"},
                {"prefix": "/dashboard"},
            ],
        },
        {"name": "partner_service", "host": "127.0.0.1", "port": 18082, "route_prefix": "/partner/"},
        {"name": "labs_service", "host": "127.0.0.1", "port": 18083, "route_prefix": "/labs/"},
    ]
}


def _wait_until_up(url: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            httpx.get(url, timeout=0.5)
            return
        except httpx.TransportError as e:
            last_err = e
            time.sleep(0.1)
    raise RuntimeError(f"{url} did not come up in time: {last_err}")


@pytest.fixture(scope="module", autouse=True)
def stack(tmp_path_factory):
    tmp_dir = tmp_path_factory.mktemp("osp_e2e")

    # Isolated copy of the seed skills_store so publish tests never write
    # into (or collide across runs with) the repo's committed example data.
    isolated_store = tmp_dir / "skills_store"
    shutil.copytree(REPO_ROOT / "registry_server" / "skills_store", isolated_store)

    registry_env = {
        **os.environ,
        "OSP_REGISTRY_DB": str(tmp_dir / "registry.db"),
        "OSP_SKILLS_STORE": str(isolated_store),
    }

    procs = []

    def spawn(module: str, port: int, env=None):
        p = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", f"{module}:app", "--port", str(port), "--log-level", "warning"],
            cwd=REPO_ROOT,
            env=env if env is not None else os.environ,
        )
        procs.append(p)
        return p

    spawn("registry_server.main", 18079, env=registry_env)
    spawn("partner_service.main", 18082)
    spawn("labs_service.main", 18083)
    for port in (18079, 18082, 18083):
        _wait_until_up(f"http://127.0.0.1:{port}/openapi.json")

    envoy_config = build_config(TEST_SPEC)
    # Distinct admin port from the dev instance's 9901.
    envoy_config["admin"]["address"]["socket_address"]["port_value"] = 19902
    envoy_config["static_resources"]["listeners"][0]["address"]["socket_address"]["port_value"] = 18010
    config_path = tmp_dir / "envoy.yaml"
    config_path.write_text(yaml.safe_dump(envoy_config, sort_keys=False))

    envoy_proc = subprocess.Popen(
        ["envoy", "-c", str(config_path), "--base-id", "1", "--log-level", "warning"],
    )
    procs.append(envoy_proc)
    _wait_until_up(f"{GATEWAY_URL}/internal/authz")  # expect 404, just needs to be listening

    yield

    for p in procs:
        p.terminate()
    for p in procs:
        p.wait(timeout=10)


@pytest.fixture()
def account():
    resp = httpx.post(f"{GATEWAY_URL}/accounts", json={"name": "pytest-account"})
    resp.raise_for_status()
    return resp.json()


@pytest.fixture()
def orchestrator(account):
    with SkillwardOrchestrator(GATEWAY_URL, api_key=account["api_key"]) as orch:
        yield orch


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
