"""Hermes: reference orchestrator for the Open Skill Protocol.

Ties together discover -> fetch -> verify -> capability-check -> execute.
"""
from __future__ import annotations

import logging
import os

import jsonschema

from .manifest import SkillManifest, SkillSummary
from .registry_client import RegistryClient
from .sandbox import ResourceLimits, SandboxRequest, SandboxRunner, SubprocessSandboxRunner

log = logging.getLogger("hermes")


class CapabilityDeniedError(Exception):
    """Raised when a skill declares a capability this deployment won't grant."""


class SchemaValidationError(Exception):
    """Raised when input or output doesn't match the manifest's declared schema."""


class HermesOrchestrator:
    """Discovers, fetches, verifies, and executes OSP skills against a registry.

    `allowed_capabilities` is this deployment's policy: the set of capability
    strings it is willing to grant to *any* skill, independent of what a
    skill's manifest asks for. A skill whose manifest requests something
    outside this set is refused before its payload is ever executed.
    """

    def __init__(
        self,
        gateway_url: str,
        *,
        api_key: str | None = None,
        allowed_capabilities: set[str] | None = None,
        sandbox: SandboxRunner | None = None,
        require_signature: bool = False,
    ):
        """`gateway_url` should point at the Envoy gateway (e.g.
        http://127.0.0.1:10000), not the registry directly — every OSP
        endpoint now requires authentication, which only the gateway
        enforces. `api_key` is required for anything but the registry's
        public /accounts signup route."""
        self.registry = RegistryClient(gateway_url, api_key=api_key)
        self.allowed_capabilities = allowed_capabilities or set()
        self.sandbox = sandbox or SubprocessSandboxRunner()
        self.require_signature = require_signature

    def discover(self, query: str = "", capability: str | None = None) -> list[SkillSummary]:
        return self.registry.discover(query, capability)

    def invoke(self, skill_id: str, version: str, input_data: dict) -> dict:
        manifest = self.registry.get_manifest(skill_id, version)
        return self.invoke_manifest(manifest, input_data)

    def invoke_manifest(self, manifest: SkillManifest, input_data: dict) -> dict:
        self._check_capabilities(manifest)

        jsonschema.validate(instance=input_data, schema=manifest.input_schema)

        payload = self.registry.fetch_verified_payload(
            manifest, require_signature=self.require_signature
        )
        log.info(
            "verified payload for %s@%s (sha256=%s)",
            manifest.id,
            manifest.version,
            manifest.payload.sha256[:12],
        )

        _, func = manifest.entrypoint_parts()  # v0.1 payloads are a single file
        granted_env = self._granted_env(manifest)

        try:
            result = self.sandbox.run(
                SandboxRequest(
                    code=payload.decode("utf-8"),
                    function=func,
                    input_data=input_data,
                    granted_env=granted_env,
                    limits=manifest.resource_limits,
                )
            )
            jsonschema.validate(instance=result, schema=manifest.output_schema)
        except Exception as e:
            self.registry.report_invocation(manifest.id, manifest.version, success=False, error=str(e))
            raise

        self.registry.report_invocation(manifest.id, manifest.version, success=True)
        return result

    def _check_capabilities(self, manifest: SkillManifest) -> None:
        for cap in manifest.capabilities:
            if cap == "none":
                continue
            if cap not in self.allowed_capabilities:
                raise CapabilityDeniedError(
                    f"{manifest.id}@{manifest.version} requests capability {cap!r}, "
                    f"which this orchestrator is not configured to grant"
                )
        net_caps = [c for c in manifest.capabilities if c.startswith("net:")]
        if net_caps:
            log.warning(
                "%s@%s was granted network capabilities %s, but the subprocess sandbox "
                "does not enforce network isolation at the OS level in v0.1 — see "
                "spec/SPEC.md's Sandboxing section before trusting this in production",
                manifest.id,
                manifest.version,
                net_caps,
            )

    def _granted_env(self, manifest: SkillManifest) -> dict[str, str]:
        granted = {}
        for cap in manifest.capabilities:
            if cap.startswith("env:"):
                var_name = cap.split(":", 1)[1]
                if var_name in os.environ:
                    granted[var_name] = os.environ[var_name]
        return granted

    def close(self) -> None:
        self.registry.close()

    def __enter__(self) -> "HermesOrchestrator":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
