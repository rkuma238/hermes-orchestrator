"""Execution backends for Skillward skills.

IMPORTANT — read spec/SPEC.md's "Sandboxing" section before deploying this
against untrusted third-party skills. SubprocessSandboxRunner gives you
process isolation, a scrubbed environment, resource limits, and a timeout —
it does NOT give you a kernel-enforced network/filesystem boundary. For
skills you don't fully trust, implement SandboxRunner with a container
(`--network=none --read-only`) or a Wasm/WASI runtime instead; nothing else
in this package needs to change.

Skill source is never written to disk: it's piped to a fresh, isolated
subprocess over stdin alongside the JSON input, executed in an ephemeral
namespace, and the subprocess exits when the call returns. This isn't a
security control — it's just that a per-call subprocess has no reason to
persist the code as a file.

Two runtimes ship today: Python (`python3.1x`) and Node.js (`node20`). Both
are single-shot: one envelope in, one JSON result out, process exits. Chain
calls (see orchestrator.py) are driven entirely by the orchestrator
inspecting a skill's *output* for a reserved `call_next` shape between hops
— nothing in this module needs to know chaining exists at all, which is why
it works identically for every runtime with no runtime-specific protocol.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass

from .manifest import ResourceLimits

_PYTHON_BOOTSTRAP = r"""
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

_NODE_BOOTSTRAP = r"""
const vm = require('vm');
let inputData = '';
process.stdin.on('data', d => inputData += d);
process.stdin.on('end', () => {
  try {
    const envelope = JSON.parse(inputData);
    const sandbox = {};
    vm.createContext(sandbox);
    new vm.Script(envelope.code, { filename: 'skill.js' }).runInContext(sandbox, { timeout: 30000 });
    const fn = sandbox[envelope.function];
    if (typeof fn !== 'function') {
      throw new Error("function '" + envelope.function + "' not found in skill code");
    }
    const result = fn(envelope.input);
    process.stdout.write(JSON.stringify({ ok: true, result: result }));
  } catch (e) {
    process.stdout.write(JSON.stringify({ ok: false, error: String((e && e.message) || e) }));
    process.exitCode = 1;
  }
});
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
    runtime: str = "python3.13"


class SandboxRunner(ABC):
    @abstractmethod
    def run(self, request: SandboxRequest) -> dict:
        """Execute one skill invocation and return its JSON-serializable result.

        The returned dict may be a reserved `call_next` shape (see
        orchestrator.py) instead of a "real" result — this module doesn't
        know or care; interpreting it is entirely the orchestrator's job."""


class SubprocessSandboxRunner(SandboxRunner):
    """Reference v0.1 backend: isolated subprocess per runtime, no persisted files."""

    def run(self, request: SandboxRequest) -> dict:
        if request.runtime.startswith("python"):
            return self._run_python(request)
        if request.runtime.startswith("node"):
            return self._run_node(request)
        raise SkillExecutionError(f"unsupported runtime: {request.runtime!r}")

    def _run_python(self, request: SandboxRequest) -> dict:
        envelope = json.dumps(
            {
                "code": request.code,
                "function": request.function,
                "input": request.input_data,
                "max_memory_mb": request.limits.max_memory_mb,
            }
        )
        env = {"PATH": "/usr/bin:/bin", **request.granted_env}

        with tempfile.TemporaryDirectory(prefix="skillward-skill-") as scratch_dir:
            try:
                proc = subprocess.run(
                    [sys.executable, "-I", "-S", "-c", _PYTHON_BOOTSTRAP],
                    input=envelope,
                    capture_output=True,
                    text=True,
                    timeout=request.limits.timeout_seconds,
                    cwd=scratch_dir,
                    env=env,
                )
            except subprocess.TimeoutExpired as e:
                raise SkillExecutionError(f"skill exceeded {request.limits.timeout_seconds}s timeout") from e

        return self._parse_outcome(proc)

    def _run_node(self, request: SandboxRequest) -> dict:
        node_path = shutil.which("node")
        if not node_path:
            raise SkillExecutionError("node runtime requested but 'node' was not found on PATH")

        envelope = json.dumps({"code": request.code, "function": request.function, "input": request.input_data})
        env = {"PATH": "/usr/bin:/bin", **request.granted_env}

        with tempfile.TemporaryDirectory(prefix="skillward-skill-") as scratch_dir:
            try:
                proc = subprocess.run(
                    [node_path, "-e", _NODE_BOOTSTRAP],
                    input=envelope,
                    capture_output=True,
                    text=True,
                    timeout=request.limits.timeout_seconds,
                    cwd=scratch_dir,
                    env=env,
                )
            except subprocess.TimeoutExpired as e:
                raise SkillExecutionError(f"skill exceeded {request.limits.timeout_seconds}s timeout") from e

        return self._parse_outcome(proc)

    def _parse_outcome(self, proc: subprocess.CompletedProcess) -> dict:
        if proc.returncode != 0 and not proc.stdout.strip():
            raise SkillExecutionError(f"skill process crashed: {proc.stderr.strip()[-2000:]}")

        try:
            outcome = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise SkillExecutionError(f"skill did not return valid JSON on stdout: {proc.stdout[:500]!r}") from e

        if not outcome.get("ok"):
            raise SkillExecutionError(f"skill raised: {outcome.get('error')}")

        return outcome["result"]
