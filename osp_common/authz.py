"""Authorization logic shared by every backend behind the OSP gateway
(registry_server, partner_service, and any future one). Deliberately small
and imported rather than copy-pasted: this is the check that decides whether
a private skill leaks to the wrong account, so it should have exactly one
implementation.

Every backend that imports this trusts `x_account_id` completely — that is
only safe because Envoy's ext_authz gate is the sole way to reach any of
these backends. See spec/SPEC.md.
"""

from fastapi import HTTPException


def is_authorized(manifest: dict, account_id: str) -> bool:
    if manifest.get("visibility", "private") == "public":
        return True
    owner = (manifest.get("publisher") or {}).get("account_id")
    if owner == account_id:
        return True
    return account_id in manifest.get("allowed_accounts", [])


def require_account(x_account_id: str | None) -> str:
    if not x_account_id:
        raise HTTPException(status_code=401, detail="unauthenticated (no x-account-id from gateway)")
    return x_account_id
