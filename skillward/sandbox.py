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

A skill isn't always a single file. `SandboxRequest.files` carries every
file published with the skill (a single entry for an ordinary skill, more
for a "combo" skill — e.g. a script plus a companion SKILL.md-style text
file, bundled and versioned together); `entrypoint_file` says which one gets
run. Every file in the bundle, including the entrypoint's own, is exposed to
the running code as `__bundle__` — a plain in-memory mapping, not real
filesystem access, so a companion file never has to be written to disk to be
readable. Only the entrypoint file is ever compiled/executed; the rest are
just data as far as this module is concerned.

Three runtimes ship today: Python (`python3.1x`) and Node.js (`node20`) are
single-shot code — one envelope in, one JSON result out, process exits. A
`text` skill isn't code at all: its entrypoint file's own content (a prompt,
instructions, any static content — the spirit of a SKILL.md) is the entire
result, returned verbatim with no subprocess, no capability surface, nothing
to sandbox. Chain calls (see orchestrator.py) are driven entirely by the
orchestrator inspecting a skill's *output* for a reserved `call_next` shape
between hops — nothing in this module needs to know chaining exists at all,
which is why it works identically for every runtime with no runtime-specific
protocol.

A skill granted `net:<url-pattern>` capabilities (see orchestrator.py) gets a
real, working `__net_fetch__(url, ...)` in its execution namespace, checked
against exactly the patterns it was granted before any request goes out —
today this is the *only* thing "net:" actually does; declaring it used to be
checked at invocation time but had no effect once the skill was running. Its
result carries both `body` (best-effort utf-8 text) and `body_base64` (the
exact response bytes) — a skill fetching something binary (a PDF, say) needs
the latter, since decoding an arbitrary binary response as utf-8 corrupts it.
For
Node, this is a hard boundary: the vm context a skill runs in starts with
nothing else in it, so `__net_fetch__` is the *only* way out at all. For
Python, it isn't — `-I -S` scrubs the environment but doesn't remove stdlib
access, so code that imports `urllib` directly bypasses the pattern check
entirely. That's the same "not a kernel-enforced boundary" caveat as the rest
of this module, not a new hole: see the module-level warning above.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from .manifest import ResourceLimits

_PYTHON_BOOTSTRAP = r"""
import sys, json, types, fnmatch

def main():
    envelope = json.loads(sys.stdin.read())
    files = envelope["files"]
    code = files[envelope["entrypoint_file"]]
    func_name = envelope["function"]
    input_data = envelope["input"]
    net_patterns = envelope.get("granted_net_patterns") or []

    max_mb = envelope.get("max_memory_mb")
    if max_mb:
        try:
            import resource
            max_bytes = int(max_mb) * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (max_bytes, max_bytes))
        except Exception:
            pass  # best-effort; not supported on every platform

    def __net_fetch__(url, method="GET", headers=None, body=None, timeout=10):
        if not any(fnmatch.fnmatch(url, pattern) for pattern in net_patterns):
            raise PermissionError(f"not granted net: access to {url!r}")
        import base64, urllib.error, urllib.request
        data = body.encode("utf-8") if isinstance(body, str) else body
        req = urllib.request.Request(url, method=method, headers=headers or {}, data=data)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                status, resp_headers = resp.status, dict(resp.headers)
        except urllib.error.HTTPError as e:
            raw = e.read()
            status, resp_headers = e.code, dict(e.headers or {})
        # `body` is best-effort utf-8 text (lossy for binary responses like a
        # PDF); `body_base64` is the exact bytes, for a skill that needs them
        # byte-for-byte rather than decoded.
        return {
            "status": status,
            "headers": resp_headers,
            "body": raw.decode("utf-8", "replace"),
            "body_base64": base64.b64encode(raw).decode("ascii"),
        }

    module = types.ModuleType("skill_payload")
    # In-memory only, never written to disk: lets the entrypoint read a
    # companion file (module.__bundle__["SKILL.md"], say) without this
    # module needing any real filesystem access to provide it.
    module.__dict__["__bundle__"] = files
    module.__dict__["__net_fetch__"] = __net_fetch__
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
const http = require('http');
const https = require('https');

function globToRegExp(pattern) {
  const escaped = pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*');
  return new RegExp('^' + escaped + '$');
}

function netFetch(netPatterns, url, options) {
  options = options || {};
  return new Promise((resolve, reject) => {
    if (!netPatterns.some(p => globToRegExp(p).test(url))) {
      reject(new Error("not granted net: access to " + url));
      return;
    }
    const client = url.startsWith('https:') ? https : http;
    const req = client.request(url, {
      method: options.method || 'GET',
      headers: options.headers || {},
      timeout: (options.timeout || 10) * 1000,
    }, (res) => {
      // Collected as raw Buffer chunks (no res.setEncoding call), then
      // decoded two ways: `body` as best-effort utf-8 text, `body_base64`
      // as the exact bytes — the same body/body_base64 split as Python's
      // __net_fetch__, needed so a binary response (e.g. a PDF) survives
      // this call byte-for-byte for a skill that asks for it.
      const chunks = [];
      res.on('data', (chunk) => { chunks.push(chunk); });
      res.on('end', () => {
        const buf = Buffer.concat(chunks);
        resolve({
          status: res.statusCode,
          headers: res.headers,
          body: buf.toString('utf8'),
          body_base64: buf.toString('base64'),
        });
      });
    });
    req.on('error', reject);
    req.on('timeout', () => req.destroy(new Error('net_fetch timed out')));
    if (options.body) req.write(options.body);
    req.end();
  });
}

let inputData = '';
process.stdin.on('data', d => inputData += d);
process.stdin.on('end', () => {
  try {
    const envelope = JSON.parse(inputData);
    const code = envelope.files[envelope.entrypoint_file];
    const netPatterns = envelope.granted_net_patterns || [];
    // Same in-memory-only bundle as the Python bootstrap: __bundle__ is a
    // plain object, not a real file the running code could open by path.
    // __net_fetch__ is the *only* way this context can reach a network at
    // all — the vm context otherwise starts with nothing else in it.
    const sandbox = {
      __bundle__: envelope.files,
      __net_fetch__: (url, options) => netFetch(netPatterns, url, options),
    };
    vm.createContext(sandbox);
    new vm.Script(code, { filename: 'skill.js' }).runInContext(sandbox, { timeout: 30000 });
    const fn = sandbox[envelope.function];
    if (typeof fn !== 'function') {
      throw new Error("function '" + envelope.function + "' not found in skill code");
    }
    // Supports an entrypoint returning a plain value or a Promise (e.g. one
    // that awaits __net_fetch__), transparently either way.
    Promise.resolve(fn(envelope.input)).then((result) => {
      process.stdout.write(JSON.stringify({ ok: true, result: result }));
    }).catch((e) => {
      process.stdout.write(JSON.stringify({ ok: false, error: String((e && e.message) || e) }));
      process.exitCode = 1;
    });
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
    files: dict[str, str]
    entrypoint_file: str
    function: str
    input_data: dict
    granted_env: dict[str, str]
    limits: ResourceLimits
    runtime: str = "python3.13"
    # URL-glob patterns (the part after "net:" in the manifest's own
    # capability strings) this invocation is allowed to reach via
    # __net_fetch__ — see the module docstring above.
    granted_net_patterns: list[str] = field(default_factory=list)


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
        if request.runtime == "text":
            return self._run_text(request)
        if request.runtime.startswith("python"):
            return self._run_python(request)
        if request.runtime.startswith("node"):
            return self._run_node(request)
        raise SkillExecutionError(f"unsupported runtime: {request.runtime!r}")

    def _run_text(self, request: SandboxRequest) -> dict:
        # No subprocess, no capability surface: there's no code here to run,
        # so there's nothing to isolate. The entrypoint file's own content is
        # the answer; any other bundled files are simply not surfaced.
        return {"text": request.files[request.entrypoint_file]}

    def _run_python(self, request: SandboxRequest) -> dict:
        envelope = json.dumps(
            {
                "files": request.files,
                "entrypoint_file": request.entrypoint_file,
                "function": request.function,
                "input": request.input_data,
                "max_memory_mb": request.limits.max_memory_mb,
                "granted_net_patterns": request.granted_net_patterns,
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

        envelope = json.dumps(
            {
                "files": request.files,
                "entrypoint_file": request.entrypoint_file,
                "function": request.function,
                "input": request.input_data,
                "granted_net_patterns": request.granted_net_patterns,
            }
        )
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
