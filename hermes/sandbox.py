"""Execution backends for OSP skills.

IMPORTANT — read spec/SPEC.md's "Sandboxing" section before deploying this
against untrusted third-party skills. SubprocessSandboxRunner gives you
process isolation, a scrubbed environment, resource limits, and a timeout —
it does NOT give you a kernel-enforced network/filesystem boundary. For
skills you don't fully trust, implement SandboxRunner with a container
(`--network=none --read-only`) or a Wasm/WASI runtime instead; nothing else
in this package needs to change.

Skill source is never written to disk: it's piped to a fresh, isolated
`python -I -S` subprocess over stdin alongside the JSON input, executed in
an ephemeral module namespace, and the subprocess exits when the call
returns. This isn't a security control — it's just that a per-call
subprocess has no reason to persist the code as a file.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .manifest import ResourceLimits

_BOOTSTRAP = r"""
import sys, json, types

def main():
    envelope = json.loads(sys.stdin.read())
    code = envelope["code"]
    func_name = envelope["function"]
    input_data = envelope["input"]

    max_mb = envelope.get("max_memory_mb")
    if max_mb:
        try:
            import resource
            max_bytes = int(max_mb) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (max_bytes, max_bytes))
        except Exception:
            pass  # best-effort; not supported on every platform

    module = types.ModuleType("skill_payload")
    exec(compile(code, "<skill>", "exec"), module.__dict__)
    func = getattr(module, func_name)
    result = func(input_data)
    sys.stdout.write(json.dumps({"ok": True, "result": result}))

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.stdout.write(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        sys.exit(1)
"""


class SkillExecutionError(Exception):
    """Raised when a skill's entrypoint raises, times out, or returns malformed output."""


@dataclass
class SandboxRequest:
    code: str
    function: str
    input_data: dict
    granted_env: dict[str, str]
    limits: ResourceLimits


class SandboxRunner(ABC):
    @abstractmethod
    def run(self, request: SandboxRequest) -> dict:
        """Execute one skill invocation and return its JSON-serializable result."""


class SubprocessSandboxRunner(SandboxRunner):
    """Reference v0.1 backend: isolated CPython subprocess, no persisted files."""

    def run(self, request: SandboxRequest) -> dict:
        envelope = json.dumps(
            {
                "code": request.code,
                "function": request.function,
                "input": request.input_data,
                "max_memory_mb": request.limits.max_memory_mb,
            }
        )

        env = {"PATH": "/usr/bin:/bin", **request.granted_env}

        with tempfile.TemporaryDirectory(prefix="osp-skill-") as scratch_dir:
            try:
                proc = subprocess.run(
                    [sys.executable, "-I", "-S", "-c", _BOOTSTRAP],
                    input=envelope,
                    capture_output=True,
                    text=True,
                    timeout=request.limits.timeout_seconds,
                    cwd=scratch_dir,
                    env=env,
                )
            except subprocess.TimeoutExpired as e:
                raise SkillExecutionError(f"skill exceeded {request.limits.timeout_seconds}s timeout") from e

        if proc.returncode != 0 and not proc.stdout.strip():
            raise SkillExecutionError(f"skill process crashed: {proc.stderr.strip()[-2000:]}")

        try:
            outcome = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise SkillExecutionError(f"skill did not return valid JSON on stdout: {proc.stdout[:500]!r}") from e

        if not outcome.get("ok"):
            raise SkillExecutionError(f"skill raised: {outcome.get('error')}")

        return outcome["result"]
