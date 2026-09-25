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

Two runtimes ship today: Python (`python3.1x`) and Node.js (`node20`). Only
Python supports chain calls (one skill invoking another via `call_skill()`)
— see "Chain calls" below. Adding a third runtime means adding a bootstrap
script and a dispatch branch in SubprocessSandboxRunner.run(); nothing else
in the orchestrator needs to change, since runtime dispatch is entirely
internal to this module.

Chain calls: the Python bootstrap exposes a `call_skill(id, version, input)`
builtin inside the skill's exec namespace. Calling it writes a
`{"type": "call_skill", ...}` line to stdout and blocks reading a response
line from stdin — the parent (SubprocessSandboxRunner) is on the other end
of that pipe, and is the only thing that can actually decide whether the
call is allowed (it has the calling manifest's declared capabilities, the
deployment's capability policy, and the chain-depth counter; the sandboxed
subprocess has none of that and is not trusted to enforce it itself). This
is why the child never gets raw network access to call back into the
registry itself — the only channel it has is this narrow, structured
request/response protocol back to its trusted parent.
"""

from __future__ import annotations

import json
import select
import shutil
import subprocess
import sys
import tempfile
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field

from .manifest import ResourceLimits

_PYTHON_BOOTSTRAP = r"""
import sys, json, types

def main():
    envelope = json.loads(sys.stdin.readline())
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

    def call_skill(id, version, input_data=None):
        request = {"type": "call_skill", "id": id, "version": version, "input": input_data or {}}
        sys.stdout.write(json.dumps(request) + "\n")
        sys.stdout.flush()
        response_line = sys.stdin.readline()
        if not response_line:
            raise RuntimeError("no response from orchestrator for chained call")
        response = json.loads(response_line)
        if not response.get("ok"):
            raise RuntimeError(response.get("error", "chained call failed"))
        return response["result"]

    module = types.ModuleType("skill_payload")
    module.__dict__["call_skill"] = call_skill
    exec(compile(code, "<skill>", "exec"), module.__dict__)
    func = getattr(module, func_name)
    result = func(input_data)
    sys.stdout.write(json.dumps({"type": "final", "ok": True, "result": result}) + "\n")
    sys.stdout.flush()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.stdout.write(json.dumps({"type": "final", "ok": False, "error": f"{type(e).__name__}: {e}"}) + "\n")
        sys.stdout.flush()
        sys.exit(1)
"""

# Single-shot: no call_skill support for Node yet (see module docstring).
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
    process.stdout.write(JSON.stringify({ type: 'final', ok: true, result: result }));
  } catch (e) {
    process.stdout.write(JSON.stringify({ type: 'final', ok: false, error: String((e && e.message) || e) }));
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
    # Only honored by the Python runtime. None means chain calls are refused
    # (the bootstrap's call_skill() gets an error response) rather than
    # silently doing nothing.
    on_call_skill: Callable[[str, str, dict], dict] | None = field(default=None)


class SandboxRunner(ABC):
    @abstractmethod
    def run(self, request: SandboxRequest) -> dict:
        """Execute one skill invocation and return its JSON-serializable result."""


class SubprocessSandboxRunner(SandboxRunner):
    """Reference v0.1 backend: isolated subprocess per runtime, no persisted files."""

    def run(self, request: SandboxRequest) -> dict:
        if request.runtime.startswith("python"):
            return self._run_python(request)
        if request.runtime.startswith("node"):
            return self._run_node(request)
        raise SkillExecutionError(f"unsupported runtime: {request.runtime!r}")

    # -- Python: interactive, supports chain calls -------------------------

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
        deadline = time.monotonic() + request.limits.timeout_seconds

        with tempfile.TemporaryDirectory(prefix="skillward-skill-") as scratch_dir:
            proc = subprocess.Popen(
                [sys.executable, "-I", "-S", "-c", _PYTHON_BOOTSTRAP],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=scratch_dir,
                env=env,
            )
            try:
                return self._pump_python(proc, envelope, request, deadline)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def _pump_python(
        self, proc: subprocess.Popen, envelope: str, request: SandboxRequest, deadline: float
    ) -> dict:
        proc.stdin.write(envelope + "\n")
        proc.stdin.flush()

        final_outcome = None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SkillExecutionError(f"skill exceeded {request.limits.timeout_seconds}s timeout")
            ready, _, _ = select.select([proc.stdout], [], [], remaining)
            if not ready:
                raise SkillExecutionError(f"skill exceeded {request.limits.timeout_seconds}s timeout")

            line = proc.stdout.readline()
            if line == "":
                break  # EOF: process is exiting
            line = line.strip()
            if not line:
                continue

            try:
                msg = json.loads(line)
            except json.JSONDecodeError as e:
                raise SkillExecutionError(f"skill wrote non-JSON output: {line[:500]!r}") from e

            if msg.get("type") == "call_skill":
                response = self._handle_call_skill(msg, request)
                proc.stdin.write(json.dumps(response) + "\n")
                proc.stdin.flush()
                continue
            if msg.get("type") == "final":
                final_outcome = msg
                break

        if final_outcome is None:
            stderr = (proc.stderr.read() or "").strip()
            raise SkillExecutionError(f"skill process ended without a result: {stderr[-2000:]}")

        if not final_outcome.get("ok"):
            raise SkillExecutionError(f"skill raised: {final_outcome.get('error')}")

        return final_outcome["result"]

    def _handle_call_skill(self, msg: dict, request: SandboxRequest) -> dict:
        if request.on_call_skill is None:
            return {"ok": False, "error": "this skill runtime does not support chained calls"}
        try:
            result = request.on_call_skill(msg["id"], msg["version"], msg.get("input") or {})
            return {"ok": True, "result": result}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # -- Node.js: single-shot, no chain calls yet ---------------------------

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

        if proc.returncode != 0 and not proc.stdout.strip():
            raise SkillExecutionError(f"skill process crashed: {proc.stderr.strip()[-2000:]}")

        try:
            outcome = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise SkillExecutionError(f"skill did not return valid JSON on stdout: {proc.stdout[:500]!r}") from e

        if not outcome.get("ok"):
            raise SkillExecutionError(f"skill raised: {outcome.get('error')}")

        return outcome["result"]
