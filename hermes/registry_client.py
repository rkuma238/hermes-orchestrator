"""Discovery + fetch side of OSP: talk to a registry, verify what it returns."""

from __future__ import annotations

import hashlib
import logging

import httpx

from .manifest import SkillManifest, SkillSummary

log = logging.getLogger("hermes")


class ChecksumMismatchError(Exception):
    """Raised when fetched payload bytes don't match the manifest's sha256."""


class SignatureMissingError(Exception):
    """Raised when a strict-signature policy requires a signature that isn't present."""


class RegistryClient:
    """Reference OSP client: discover, fetch manifest, fetch+verify payload."""

    def __init__(self, base_url: str, *, api_key: str | None = None, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.Client(timeout=timeout, headers=headers)

    def discover(self, query: str = "", capability: str | None = None) -> list[SkillSummary]:
        params = {}
        if query:
            params["q"] = query
        if capability:
            params["capability"] = capability
        resp = self._client.get(f"{self.base_url}/discover", params=params)
        resp.raise_for_status()
        return [SkillSummary.model_validate(item) for item in resp.json()]

    def get_manifest(self, skill_id: str, version: str) -> SkillManifest:
        resp = self._client.get(f"{self.base_url}/skills/{skill_id}/{version}/manifest")
        resp.raise_for_status()
        return SkillManifest.model_validate(resp.json())

    def fetch_verified_payload(self, manifest: SkillManifest, *, require_signature: bool = False) -> bytes:
        """Fetch the payload bytes for a manifest and verify integrity before returning.

        Raises ChecksumMismatchError if the fetched bytes don't match
        manifest.payload.sha256 — callers MUST NOT execute the bytes if this
        raises.
        """
        url = manifest.payload.url
        if url.startswith("/"):
            url = f"{self.base_url}{url}"
        resp = self._client.get(url)
        resp.raise_for_status()
        payload = resp.content

        digest = hashlib.sha256(payload).hexdigest()
        if digest != manifest.payload.sha256:
            raise ChecksumMismatchError(
                f"payload for {manifest.id}@{manifest.version} failed integrity check: "
                f"expected sha256={manifest.payload.sha256}, got {digest}"
            )

        if require_signature and not manifest.payload.signature:
            raise SignatureMissingError(
                f"{manifest.id}@{manifest.version} has no signature but policy requires one"
            )
        if manifest.payload.signature and manifest.publisher and manifest.publisher.public_key:
            _verify_signature(digest, manifest.payload.signature, manifest.publisher.public_key)

        return payload

    def report_invocation(self, skill_id: str, version: str, *, success: bool, error: str | None = None) -> None:
        """Best-effort usage telemetry so a skill's publisher can see it was
        invoked. Never raises — a telemetry failure must not fail the call
        that already succeeded (or already failed on its own terms)."""
        try:
            self._client.post(
                f"{self.base_url}/skills/{skill_id}/{version}/invocations",
                json={"success": success, "error": error},
                timeout=2.0,
            )
        except httpx.HTTPError as e:
            log.warning("failed to report invocation telemetry for %s@%s: %s", skill_id, version, e)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RegistryClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _verify_signature(digest_hex: str, signature_b64: str, public_key_b64: str) -> None:
    """Verify an Ed25519 signature over the sha256 digest. Raises on failure."""
    import base64

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    pubkey = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
    try:
        pubkey.verify(base64.b64decode(signature_b64), bytes.fromhex(digest_hex))
    except InvalidSignature as e:
        raise ChecksumMismatchError("payload signature verification failed") from e
