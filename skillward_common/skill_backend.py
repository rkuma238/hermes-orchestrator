"""Factory for a minimal Skillward skill backend: serves GET .../{id}/{version}/manifest
and .../payload out of a local skills_store, enforcing the shared
visibility/ACL check (is_authorized). Every non-primary skill backend
(partner_service, labs_service, and any future one) is just a few lines
calling this — the point being that onboarding a new registry behind the
gateway is 'write a skills_store + one backends.yaml entry', not
copy-pasted security logic.
"""

import json
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import PlainTextResponse

from .authz import is_authorized, require_account


def make_skill_backend_app(*, title: str, route_prefix: str, store_dir: Path) -> FastAPI:
    prefix = route_prefix.rstrip("/")
    app = FastAPI(title=title, version="0.1")

    def _load_manifest_or_none(skill_id: str, version: str) -> dict | None:
        manifest_path = store_dir / skill_id / version / "manifest.json"
        if not manifest_path.exists():
            return None
        return json.loads(manifest_path.read_text())

    @app.get(prefix + "/{skill_id}/{version}/manifest")
    def get_manifest(skill_id: str, version: str, x_account_id: str | None = Header(default=None)) -> dict:
        account_id = require_account(x_account_id)
        manifest = _load_manifest_or_none(skill_id, version)
        if not manifest or not is_authorized(manifest, account_id):
            raise HTTPException(status_code=404, detail=f"no such skill {skill_id}@{version}")
        return manifest

    @app.get(prefix + "/{skill_id}/{version}/payload")
    def get_payload(
        skill_id: str, version: str, x_account_id: str | None = Header(default=None)
    ) -> PlainTextResponse:
        account_id = require_account(x_account_id)
        manifest = _load_manifest_or_none(skill_id, version)
        if not manifest or not is_authorized(manifest, account_id):
            raise HTTPException(status_code=404, detail=f"no such skill {skill_id}@{version}")
        entrypoint_file, _ = manifest["entrypoint"].split(":")
        payload_path = store_dir / skill_id / version / entrypoint_file
        if not payload_path.exists():
            raise HTTPException(status_code=404, detail="payload file missing")
        return PlainTextResponse(payload_path.read_text(), media_type="text/x-python")

    return app
