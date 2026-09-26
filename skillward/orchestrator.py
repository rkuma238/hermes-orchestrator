"""Skillward: reference orchestrator for the Skillward Protocol.

Ties together discover -> fetch -> verify -> capability-check -> execute.
"""

from __future__ import annotations

import logging
import os
import time

import jsonschema

from .manifest import ResourceLimits, SkillManifest, SkillSummary
from .registry_client import RegistryClient
from .sandbox import SandboxRequest, SandboxRunner, SkillExecutionError, SubprocessSandboxRunner

log = logging.getLogger("skillward")

#: A skill hands off to another skill by returning *exactly* this shape as
#: its result — nothing else at the top level. Reserved by the protocol;
#: skill authors must not use a "call_next" key for any other purpose. See
#: SkillwardOrchestrator.invoke_manifest and spec/SPEC.md's "Chain calls".
_CALL_NEXT_KEY = "call_next"


class CapabilityDeniedError(Exception):
    """Raised when a skill declares a capability this deployment won't grant."""


class SchemaValidationError(Exception):
    """Raised when input or output doesn't match the manifest's declared schema."""


class ChainDepthExceededError(Exception):
    """Raised when a chain of skill-handing-off-to-skill exceeds SkillwardOrchestrator.MAX_CHAIN_DEPTH."""


def _extract_call_next(result: dict) -> dict | None:
    """Returns the {"id", "version", "input"} hand-off request if `result` is
    exactly a reserved call_next shape, else None (meaning: treat `result`
    as this skill's real, final output)."""
    if not isinstance(result, dict) or set(result.keys()) != {_CALL_NEXT_KEY}:
        return None
    next_call = result[_CALL_NEXT_KEY]
    if not isinstance(next_call, dict) or "id" not in next_call or "version" not in next_call:
        raise SkillExecutionError(
            f"malformed {_CALL_NEXT_KEY!r}: expected {{'id', 'version', 'input'?}}, got {next_call!r}"
        )
    return {"id": next_call["id"], "version": next_call["version"], "input": next_call.get("input") or {}}


class SkillwardOrchestrator:
    """Discovers, fetches, verifies, and executes Skillward skills against a registry.

    `allowed_capabilities` is this deployment's policy: the set of capability
    strings it is willing to grant to *any* skill, independent of what a
    skill's manifest asks for. A skill whose manifest requests something
    outside this set is refused before its payload is ever executed.
    """

    #: A skill can hand off to another skill by returning a call_next result
    #: (see _extract_call_next). This caps how many hand-offs one top-level
    #: invoke() will follow before giving up, so a cycle or a runaway chain
    #: can't loop forever. This loop is driven entirely by this orchestrator
    #: — no skill's own code stays running while a hand-off happens; it
    #: already returned and its process already exited.
    MAX_CHAIN_DEPTH = 5

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
        http://127.0.0.1:10000), not the registry directly — every Skillward
        endpoint now requires authentication, which only the gateway
        enforces. `api_key` is required for anything but the registry's
        public /accounts signup route."""
        self.registry = RegistryClient(gateway_url, api_key=api_key)
        self.allowed_capabilities = allowed_capabilities or set()
        self.sandbox = sandbox or SubprocessSandboxRunner()
        self.require_signature = require_signature

    def discover(self, query: str = "", capability: str | None = None) -> list[SkillSummary]:
        return self.registry.discover(query, capability)

    def list_versions(self, skill_id: str) -> list[str]:
        return self.registry.list_versions(skill_id)

    def invoke(self, skill_id: str, version: str, input_data: dict) -> dict:
        """`version` may be an exact semver, or "latest" to always run
        whatever's currently published — resolved centrally by the registry,
        not by whatever happens to be sitting in a local skills directory."""
        manifest = self.registry.get_manifest(skill_id, version)
        return self.invoke_manifest(manifest, input_data)

    def invoke_manifest(self, manifest: SkillManifest, input_data: dict) -> dict:
        """Runs `manifest`, following any call_next hand-offs it returns,
        until a hop returns a real result (or the chain-depth/shared-
        deadline limits are hit). One shared wall-clock deadline, computed
        from *this* manifest's own resource_limits, governs every hop in
        the chain — a later hop's own resource_limits can only shrink its
        remaining budget further, never extend the chain past this deadline.

        Every hop after the first receives the full history of prior hops
        in this chain — not just whatever the previous hop chose to forward
        as its own call_next input — via the reserved `_chain_context` input
        key (see _run_one_hop).
        """
        deadline = time.monotonic() + manifest.resource_limits.timeout_seconds
        current_manifest = manifest
        current_input = input_data
        chain_context: list[dict] = []

        for hop in range(self.MAX_CHAIN_DEPTH + 1):
            result = self._run_one_hop(current_manifest, current_input, chain_context, deadline)

            next_call = _extract_call_next(result)
            if next_call is None:
                jsonschema.validate(instance=result, schema=current_manifest.output_schema)
                return result

            self._check_chain_permission(current_manifest, next_call["id"])
            chain_context = [
                *chain_context,
                {"id": current_manifest.id, "version": current_manifest.version, "output": result},
            ]
            log.info(
                "%s@%s handed off to %s (hop %d/%d)",
                current_manifest.id,
                current_manifest.version,
                next_call["id"],
                hop + 1,
                self.MAX_CHAIN_DEPTH,
            )
            current_manifest = self.registry.get_manifest(next_call["id"], next_call["version"])
            current_input = next_call["input"]

        raise ChainDepthExceededError(
            f"chain starting at {manifest.id}@{manifest.version} exceeded max depth ({self.MAX_CHAIN_DEPTH})"
        )

    def _run_one_hop(
        self, manifest: SkillManifest, input_data: dict, chain_context: list[dict], deadline: float
    ) -> dict:
        self._check_capabilities(manifest)
        jsonschema.validate(instance=input_data, schema=manifest.input_schema)

        payload = self.registry.fetch_verified_payload(manifest, require_signature=self.require_signature)
        log.info(
            "verified payload for %s@%s (sha256=%s)",
            manifest.id,
            manifest.version,
            manifest.payload.sha256[:12],
        )

        _, func = manifest.entrypoint_parts()  # v0.1 payloads are a single file
        granted_env = self._granted_env(manifest)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            self.registry.report_invocation(manifest.id, manifest.version, success=False, error="chain timed out")
            raise SkillExecutionError(f"chain timed out before {manifest.id}@{manifest.version} could run")
        effective_limits = ResourceLimits(
            timeout_seconds=min(remaining, manifest.resource_limits.timeout_seconds),
            max_memory_mb=manifest.resource_limits.max_memory_mb,
        )

        # _chain_context is injected *after* validating input_data against
        # the manifest's own input_schema, so a skill's schema never needs
        # to account for it — it's an addition on top of whatever input the
        # skill actually declared, not a replacement for it. Only added past
        # the first hop, so a skill invoked standalone (never chained) sees
        # exactly the input it always did.
        sandbox_input = dict(input_data)
        if chain_context:
            sandbox_input["_chain_context"] = chain_context

        try:
            result = self.sandbox.run(
                SandboxRequest(
                    code=payload.decode("utf-8"),
                    function=func,
                    input_data=sandbox_input,
                    granted_env=granted_env,
                    limits=effective_limits,
                    runtime=manifest.runtime,
                )
            )
        except Exception as e:
            self.registry.report_invocation(manifest.id, manifest.version, success=False, error=str(e))
            raise

        self.registry.report_invocation(manifest.id, manifest.version, success=True)
        return result

    def _check_chain_permission(self, calling_manifest: SkillManifest, called_id: str) -> None:
        """A skill may only hand off to a target it declared via skill:<id>
        or skill:* in its own capabilities — checked here, by the
        orchestrator, after the hop already ran and asked to hand off. The
        skill's own code never gets to decide this for itself."""
        allowed_targets = {
            cap.split(":", 1)[1] for cap in calling_manifest.capabilities if cap.startswith("skill:")
        }
        if called_id not in allowed_targets and "*" not in allowed_targets:
            raise CapabilityDeniedError(
                f"{calling_manifest.id}@{calling_manifest.version} is not permitted to hand off to "
                f"skill {called_id!r} (declare 'skill:{called_id}' or 'skill:*' in its capabilities)"
            )

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

    def __enter__(self) -> SkillwardOrchestrator:
        return self

    def __exit__(self, *exc) -> None:
        self.close()
