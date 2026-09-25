"""Reference Skillward registry.

Sits behind the Envoy gateway (envoy/envoy.yaml), which authenticates every
request via ext_authz -> /internal/authz before forwarding it here with a
trusted `x-account-id` header. This app is not meant to be reachable
directly in a real deployment — /internal/authz in particular must never be
exposed on the public listener (Envoy's config blocks it explicitly).

Authorization (which skills a given account can see) is enforced here, using
the identity Envoy already validated.
"""

import hashlib
import json
import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from skillward_common import is_authorized, require_account

from . import db

_DEFAULT_STORE = Path(__file__).parent / "skills_store"
# Overridable so tests (or a second registry instance) can publish into an
# isolated directory instead of this repo's committed example skills_store.
STORE = Path(os.environ["OSP_SKILLS_STORE"]) if "OSP_SKILLS_STORE" in os.environ else _DEFAULT_STORE
DASHBOARD_DIR = Path(__file__).parent.parent / "dashboard"

app = FastAPI(title="Skillward Reference Registry", version="0.2")

# Dev convenience only, so the static dashboard (served from a different
# origin/port) can call the registry directly while testing without Envoy.
# The gateway is still the intended production entry point and enforces auth
# regardless of what CORS allows here.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    db.init_db()


# Served same-origin through the gateway at /dashboard/ — the dashboard's own
# fetch() calls use relative paths, so no CORS is needed for it to work.
app.mount("/dashboard", StaticFiles(directory=DASHBOARD_DIR, html=True), name="dashboard")


# ---------------------------------------------------------------------------
# Accounts (signup is the one route Envoy leaves unauthenticated)
# ---------------------------------------------------------------------------


@app.post("/accounts")
def create_account(body: dict):
    name = body.get("name")
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    account_id, api_key = db.create_account(name)
    return {"account_id": account_id, "api_key": api_key}


# ---------------------------------------------------------------------------
# Internal: called by Envoy's ext_authz filter on every gated request.
# Never routed externally — see envoy/envoy.yaml.
# ---------------------------------------------------------------------------


@app.api_route(
    "/internal/authz{_rest:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
)
def authz_check(_rest: str, authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    api_key = authorization.removeprefix("Bearer ").strip()
    account_id = db.resolve_api_key(api_key)
    if not account_id:
        raise HTTPException(status_code=401, detail="invalid api key")
    # Envoy copies this response header onto the request it forwards upstream
    # (see authorization_response.allowed_upstream_headers in envoy.yaml).
    return Response(status_code=200, headers={"x-account-id": account_id})


# ---------------------------------------------------------------------------
# Skill storage helpers + authorization
# ---------------------------------------------------------------------------


def _load_all_manifests() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(STORE.glob("*/*/manifest.json"))]


def _load_manifest_or_none(skill_id: str, version: str) -> dict | None:
    manifest_path = STORE / skill_id / version / "manifest.json"
    if not manifest_path.exists():
        return None
    return json.loads(manifest_path.read_text())


# ---------------------------------------------------------------------------
# Discovery / manifest / payload — all require auth + per-skill authorization
# ---------------------------------------------------------------------------


@app.get("/discover")
def discover(
    q: str = "",
    capability: str | None = None,
    x_account_id: str | None = Header(default=None),
) -> list[dict]:
    account_id = require_account(x_account_id)
    results = []
    for manifest in _load_all_manifests():
        if not is_authorized(manifest, account_id):
            continue
        haystack = f"{manifest['name']} {manifest['description']}".lower()
        if q and q.lower() not in haystack:
            continue
        if capability and capability not in manifest["capabilities"]:
            continue
        results.append(
            {
                "id": manifest["id"],
                "version": manifest["version"],
                "name": manifest["name"],
                "description": manifest["description"],
                "capabilities": manifest["capabilities"],
                "input_schema": manifest["input_schema"],
                "output_schema": manifest["output_schema"],
                "visibility": manifest.get("visibility", "private"),
            }
        )
    return results


@app.get("/skills/{skill_id}/{version}/manifest")
def get_manifest(skill_id: str, version: str, x_account_id: str | None = Header(default=None)) -> dict:
    account_id = require_account(x_account_id)
    manifest = _load_manifest_or_none(skill_id, version)
    # 404 (not 403) whether the skill is missing or just not authorized for
    # this caller, so discovery can't be used to probe private skill ids.
    if not manifest or not is_authorized(manifest, account_id):
        raise HTTPException(status_code=404, detail=f"no such skill {skill_id}@{version}")
    return manifest


@app.get("/skills/{skill_id}/{version}/payload")
def get_payload(skill_id: str, version: str, x_account_id: str | None = Header(default=None)) -> PlainTextResponse:
    account_id = require_account(x_account_id)
    manifest = _load_manifest_or_none(skill_id, version)
    if not manifest or not is_authorized(manifest, account_id):
        raise HTTPException(status_code=404, detail=f"no such skill {skill_id}@{version}")
    entrypoint_file, _ = manifest["entrypoint"].split(":")
    payload_path = STORE / skill_id / version / entrypoint_file
    if not payload_path.exists():
        raise HTTPException(status_code=404, detail="payload file missing")
    return PlainTextResponse(payload_path.read_text(), media_type="text/x-python")


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


@app.post("/skills/{skill_id}/{version}")
def publish_skill(skill_id: str, version: str, body: dict, x_account_id: str | None = Header(default=None)):
    account_id = require_account(x_account_id)

    existing = _load_manifest_or_none(skill_id, version)
    if existing and (existing.get("publisher") or {}).get("account_id") != account_id:
        raise HTTPException(status_code=403, detail="skill@version already published by another account")

    required = ["name", "description", "runtime", "entrypoint", "input_schema", "output_schema", "code"]
    missing = [f for f in required if f not in body]
    if missing:
        raise HTTPException(status_code=400, detail=f"missing fields: {missing}")

    code: str = body["code"]
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest()
    entrypoint_file, _ = body["entrypoint"].split(":")

    skill_dir = STORE / skill_id / version
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / entrypoint_file).write_text(code)

    manifest = {
        "protocol_version": "0.1",
        "id": skill_id,
        "version": version,
        "name": body["name"],
        "description": body["description"],
        "runtime": body["runtime"],
        "entrypoint": body["entrypoint"],
        "input_schema": body["input_schema"],
        "output_schema": body["output_schema"],
        "capabilities": body.get("capabilities", []),
        "resource_limits": body.get("resource_limits", {"timeout_seconds": 10, "max_memory_mb": 256}),
        "payload": {
            "url": f"/skills/{skill_id}/{version}/payload",
            "sha256": digest,  # computed server-side; the publisher cannot spoof this
        },
        "publisher": {"account_id": account_id},
        "visibility": body.get("visibility", "private"),
        "allowed_accounts": body.get("allowed_accounts", []),
    }
    (skill_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


@app.get("/accounts/{account_id}/skills")
def list_account_skills(account_id: str, x_account_id: str | None = Header(default=None)) -> list[dict]:
    caller = require_account(x_account_id)
    if caller != account_id:
        raise HTTPException(status_code=403, detail="cannot view another account's skills")
    return [m for m in _load_all_manifests() if (m.get("publisher") or {}).get("account_id") == account_id]


# ---------------------------------------------------------------------------
# Invocation telemetry (best-effort, reported by orchestrators after a run)
# ---------------------------------------------------------------------------


@app.post("/skills/{skill_id}/{version}/invocations")
def report_invocation(skill_id: str, version: str, body: dict, x_account_id: str | None = Header(default=None)):
    caller_account_id = require_account(x_account_id)
    manifest = _load_manifest_or_none(skill_id, version)
    owner_account_id = (manifest.get("publisher") or {}).get("account_id") if manifest else None
    db.record_invocation(
        skill_id=skill_id,
        skill_version=version,
        owner_account_id=owner_account_id,
        caller_account_id=caller_account_id,
        success=bool(body.get("success")),
        error=body.get("error"),
    )
    return {"ok": True}


@app.get("/accounts/{account_id}/invocations")
def list_invocations(account_id: str, x_account_id: str | None = Header(default=None)) -> list[dict]:
    caller = require_account(x_account_id)
    if caller != account_id:
        raise HTTPException(status_code=403, detail="cannot view another account's invocation log")
    return db.list_invocations_for_owner(account_id)
