"""Shared fixtures: spins up registry + partner_service + labs_service +
Envoy once per test session, on dedicated test ports distinct from the dev
ports, so the suite can run alongside a manually-running dev stack.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml

from scripts.generate_envoy_config import build_config
from skillward import SkillwardOrchestrator

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


@pytest.fixture(scope="session", autouse=True)
def stack(tmp_path_factory):
    tmp_dir = tmp_path_factory.mktemp("skillward_e2e")

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
