"""Full-stack tests against the shared stack from conftest.py (registry +
partner_service + labs_service + Envoy, on dedicated test ports).

Run with: pytest tests/ -v (from the project root, inside .venv)
"""

import copy
import http.server
import json
import threading

import httpx
import jsonschema
import pytest
from conftest import GATEWAY_URL

from skillward import SkillwardOrchestrator
from skillward.orchestrator import CapabilityDeniedError
from skillward.registry_client import ChecksumMismatchError
from skillward.sandbox import SkillExecutionError


@pytest.fixture()
def local_http_server():
    # A real, local-only HTTP server so __net_fetch__ tests prove actual
    # network I/O happens, without depending on any external connectivity.
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"hello": "world"}).encode())

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


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


def test_discover_query_matches_id_and_capability(orchestrator):
    # "currency" isn't in the skill's name or description text at all — it's
    # only in the id — so this only passes if matching covers id too.
    by_id = orchestrator.discover(query="currency")
    assert any(s.id == "partner-currency-convert" for s in by_id)

    by_capability = orchestrator.discover(capability="net:https://api.example.com/*")
    # No example skill declares that capability; this should filter down to
    # nothing rather than error, proving the capability filter is applied.
    assert by_capability == []


def test_publish_is_immediately_discoverable(orchestrator, account):
    # Regression test for the manifest cache added alongside this test:
    # publishing must invalidate it, or a freshly published skill would stay
    # invisible until some other publish happened to clear the cache.
    skill_id = "pytest-cache-check"
    publish_resp = httpx.post(
        f"{GATEWAY_URL}/skills/{skill_id}/1.0.0",
        headers={"Authorization": f"Bearer {account['api_key']}"},
        json={
            "name": "Cache Check",
            "description": "proves publish invalidates the manifest cache",
            "runtime": "python3.13",
            "entrypoint": "payload.py:run",
            "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]},
            "output_schema": {"type": "object", "properties": {"y": {"type": "integer"}}, "required": ["y"]},
            "visibility": "public",
            "code": "def run(input_data):\n    return {'y': input_data['x']}\n",
        },
    )
    assert publish_resp.status_code == 200

    ids = {s.id for s in orchestrator.discover()}
    assert skill_id in ids

    result = orchestrator.invoke(skill_id, "1.0.0", {"x": 7})
    assert result == {"y": 7}


def test_node_runtime_skill(orchestrator, account):
    publish_resp = httpx.post(
        f"{GATEWAY_URL}/skills/pytest-node-skill/1.0.0",
        headers={"Authorization": f"Bearer {account['api_key']}"},
        json={
            "name": "Node Skill",
            "description": "proves the node20 runtime works end to end, not just at the sandbox level",
            "runtime": "node20",
            "entrypoint": "payload.js:run",
            "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            "output_schema": {
                "type": "object",
                "properties": {"upper": {"type": "string"}},
                "required": ["upper"],
            },
            "visibility": "public",
            "code": "function run(input) { return { upper: input.text.toUpperCase() }; }",
        },
    )
    assert publish_resp.status_code == 200
    assert publish_resp.json()["runtime"] == "node20"

    result = orchestrator.invoke("pytest-node-skill", "1.0.0", {"text": "skillward"})
    assert result == {"upper": "SKILLWARD"}


def test_text_runtime_skill(orchestrator, account):
    # Not code at all: no function, no sandbox, no capability surface — the
    # payload's own bytes are the entire result, in the spirit of a SKILL.md.
    publish_resp = httpx.post(
        f"{GATEWAY_URL}/skills/pytest-text-skill/1.0.0",
        headers={"Authorization": f"Bearer {account['api_key']}"},
        json={
            "name": "Text Skill",
            "description": "a plain-text/prompt skill with no code to execute",
            "runtime": "text",
            "entrypoint": "skill.md",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            "visibility": "public",
            "code": "You are a helpful assistant. Always respond in a friendly tone.",
        },
    )
    assert publish_resp.status_code == 200
    assert publish_resp.json()["runtime"] == "text"

    result = orchestrator.invoke("pytest-text-skill", "1.0.0", {})
    assert result == {"text": "You are a helpful assistant. Always respond in a friendly tone."}


def test_combo_skill_bundles_script_and_companion_text(orchestrator, account):
    # The realistic "SKILL.md + scripts" shape: one skill, one version, two
    # files — a script and a companion instructions file it can read at run
    # time via __bundle__, published and fetched as a single verified unit.
    publish_resp = httpx.post(
        f"{GATEWAY_URL}/skills/pytest-combo-skill/1.0.0",
        headers={"Authorization": f"Bearer {account['api_key']}"},
        json={
            "name": "Combo Skill",
            "description": "a script bundled with a companion SKILL.md-style text file",
            "runtime": "python3.13",
            "entrypoint": "run.py:run",
            "input_schema": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
            "output_schema": {
                "type": "object",
                "properties": {"greeting": {"type": "string"}, "doubled": {"type": "integer"}},
                "required": ["greeting", "doubled"],
            },
            "visibility": "public",
            "files": {
                "run.py": (
                    "def run(input_data):\n"
                    "    instructions = __bundle__['SKILL.md']\n"
                    "    return {'greeting': instructions.strip(), 'doubled': input_data['n'] * 2}\n"
                ),
                "SKILL.md": "You are a doubling assistant.",
            },
        },
    )
    assert publish_resp.status_code == 200
    assert sorted(publish_resp.json()["bundle_files"]) == ["SKILL.md", "run.py"]

    result = orchestrator.invoke("pytest-combo-skill", "1.0.0", {"n": 21})
    assert result == {"greeting": "You are a doubling assistant.", "doubled": 42}


def test_combo_skill_rejects_unsafe_bundle_paths(account):
    resp = httpx.post(
        f"{GATEWAY_URL}/skills/pytest-combo-unsafe/1.0.0",
        headers={"Authorization": f"Bearer {account['api_key']}"},
        json={
            "name": "Unsafe Combo",
            "description": "tries to escape the skill directory via a bundle path",
            "runtime": "python3.13",
            "entrypoint": "run.py:run",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object"},
            "visibility": "public",
            "files": {
                "run.py": "def run(input_data):\n    return {}\n",
                "../../etc/passwd": "nope",
            },
        },
    )
    assert resp.status_code == 400


_NET_FETCH_CODE = (
    "def run(input_data):\n"
    "    resp = __net_fetch__(input_data['url'])\n"
    "    return {'status': resp['status'], 'body': resp['body']}\n"
)


def _publish_net_fetch_skill(api_key: str, skill_id: str, net_pattern: str):
    resp = httpx.post(
        f"{GATEWAY_URL}/skills/{skill_id}/1.0.0",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "name": "Net Fetch Skill",
            "description": "makes a real outbound HTTP call via __net_fetch__",
            "runtime": "python3.13",
            "entrypoint": "run.py:run",
            "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
            "output_schema": {
                "type": "object",
                "properties": {"status": {"type": "integer"}, "body": {"type": "string"}},
                "required": ["status", "body"],
            },
            "capabilities": [f"net:{net_pattern}"],
            "visibility": "public",
            "code": _NET_FETCH_CODE,
        },
    )
    assert resp.status_code == 200
    return resp.json()


def test_net_fetch_succeeds_when_pattern_and_policy_both_grant_it(account, local_http_server):
    api_key = account["api_key"]
    pattern = f"{local_http_server}/*"
    _publish_net_fetch_skill(api_key, "pytest-net-fetch-ok", pattern)

    with SkillwardOrchestrator(GATEWAY_URL, api_key=api_key, allowed_capabilities={f"net:{pattern}"}) as orch:
        result = orch.invoke("pytest-net-fetch-ok", "1.0.0", {"url": f"{local_http_server}/data"})
    assert result["status"] == 200
    assert json.loads(result["body"]) == {"hello": "world"}


def test_net_fetch_denied_when_deployment_policy_refuses_it(account, local_http_server):
    api_key = account["api_key"]
    pattern = f"{local_http_server}/*"
    _publish_net_fetch_skill(api_key, "pytest-net-fetch-policy-denied", pattern)

    # The skill declares the capability, but this deployment doesn't grant
    # it — refused before the skill ever runs, same as any other capability.
    with SkillwardOrchestrator(GATEWAY_URL, api_key=api_key, allowed_capabilities=set()) as orch:
        with pytest.raises(CapabilityDeniedError):
            orch.invoke("pytest-net-fetch-policy-denied", "1.0.0", {"url": f"{local_http_server}/data"})


def test_net_fetch_denied_when_url_does_not_match_granted_pattern(account, local_http_server):
    api_key = account["api_key"]
    # Granted a *different* host than the one it actually tries to reach —
    # the mismatch is caught by __net_fetch__ itself, inside the sandbox.
    _publish_net_fetch_skill(api_key, "pytest-net-fetch-pattern-mismatch", "https://totally-different.example/*")

    with SkillwardOrchestrator(
        GATEWAY_URL, api_key=api_key, allowed_capabilities={"net:https://totally-different.example/*"}
    ) as orch:
        with pytest.raises(SkillExecutionError, match="not granted net"):
            orch.invoke("pytest-net-fetch-pattern-mismatch", "1.0.0", {"url": f"{local_http_server}/data"})


def test_invoke_with_latest_resolves_to_newest_version(orchestrator, account):
    # A local skills directory has no equivalent of this: a file on disk is
    # just whatever happens to be checked out, with no live "give me
    # whatever's current" concept. The registry resolves it centrally.
    api_key = account["api_key"]
    skill_id = "pytest-latest-check"
    for version, n in [("1.0.0", 1), ("1.1.0", 2)]:
        resp = httpx.post(
            f"{GATEWAY_URL}/skills/{skill_id}/{version}",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "name": "Latest Check",
                "description": "proves version='latest' resolves centrally to the newest published version",
                "runtime": "python3.13",
                "entrypoint": "payload.py:run",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
                "visibility": "public",
                "code": f"def run(input_data):\n    return {{'n': {n}}}\n",
            },
        )
        assert resp.status_code == 200

    manifest = orchestrator.registry.get_manifest(skill_id, "latest")
    assert manifest.version == "1.1.0"  # resolved to concrete, never the literal "latest"

    result = orchestrator.invoke(skill_id, "latest", {})
    assert result == {"n": 2}

    versions = orchestrator.list_versions(skill_id)
    assert versions == ["1.1.0", "1.0.0"]  # newest first


def _publish_immutability_skill(api_key: str, skill_id: str, *, code: str, visibility: str = "public"):
    return httpx.post(
        f"{GATEWAY_URL}/skills/{skill_id}/1.0.0",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "name": "Immutability Check",
            "description": "proves a published version's code can't silently change",
            "runtime": "python3.13",
            "entrypoint": "payload.py:run",
            "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]},
            "output_schema": {"type": "object", "properties": {"y": {"type": "integer"}}, "required": ["y"]},
            "visibility": visibility,
            "code": code,
        },
    )


def test_republishing_same_version_with_different_code_is_rejected(account):
    first = _publish_immutability_skill(
        account["api_key"], "pytest-immutable-1", code="def run(input_data):\n    return {'y': 1}\n"
    )
    assert first.status_code == 200
    original_digest = first.json()["payload"]["sha256"]

    second = _publish_immutability_skill(
        account["api_key"], "pytest-immutable-1", code="def run(input_data):\n    return {'y': 2}\n"
    )
    assert second.status_code == 409
    assert original_digest[:12] in second.json()["detail"]


def test_republishing_same_version_with_identical_code_is_allowed(account):
    code = "def run(input_data):\n    return {'y': 3}\n"
    first = _publish_immutability_skill(account["api_key"], "pytest-immutable-2", code=code, visibility="private")
    assert first.status_code == 200

    # Same code, different visibility — allowed, since the digest is unchanged.
    second = _publish_immutability_skill(account["api_key"], "pytest-immutable-2", code=code, visibility="public")
    assert second.status_code == 200
    assert second.json()["payload"]["sha256"] == first.json()["payload"]["sha256"]
    assert second.json()["visibility"] == "public"


def test_repeated_payload_fetch_skips_the_network(orchestrator):
    # Not just "returns the right bytes twice" — actually counting the
    # underlying HTTP calls, since a cache that's correct-but-unused would
    # pass a naive assertion just as easily as a working one.
    manifest = orchestrator.registry.get_manifest("example-echo", "1.0.0")

    call_count = 0
    real_get = orchestrator.registry._client.get

    def counting_get(url, *args, **kwargs):
        nonlocal call_count
        call_count += 1
        return real_get(url, *args, **kwargs)

    orchestrator.registry._client.get = counting_get

    first = orchestrator.registry.fetch_verified_payload(manifest)
    second = orchestrator.registry.fetch_verified_payload(manifest)
    third = orchestrator.registry.fetch_verified_payload(manifest)

    assert first == second == third
    assert call_count == 1
