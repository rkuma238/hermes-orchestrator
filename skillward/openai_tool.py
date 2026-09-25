"""Expose Skillward skills as OpenAI tool definitions.

No OpenAI SDK dependency is needed here — a "tool" for OpenAI's APIs is just
a JSON-serializable dict, and dispatching a tool call is just calling a
function with parsed arguments. Two shapes are supported:

- Responses API (`client.responses.create(tools=...)`) — flat, current,
  recommended. See `build_openai_responses_toolset`.
- Chat Completions API (`client.chat.completions.create(tools=...)`) —
  nested under a "function" key, still widely used and not deprecated.
  See `build_openai_chat_completions_toolset`.

The Assistants API is intentionally not supported: it was sunset on
2026-08-26 with no migration window, and its tool-definition shape matched
Chat Completions' nested format anyway if you still need it for archived
code.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .manifest import SkillSummary
from .orchestrator import SkillwardOrchestrator


@dataclass
class OpenAIToolset:
    """`tools` is ready to pass straight to the `tools=` parameter of either
    `client.responses.create` or `client.chat.completions.create`, matching
    whichever builder function produced this toolset. `dispatch` takes a
    tool call's name and its (JSON-string) arguments, exactly as OpenAI
    returns them, and actually invokes the matching skill."""

    tools: list[dict]
    _orchestrator: SkillwardOrchestrator
    _name_to_skill: dict[str, tuple[str, str]] = field(default_factory=dict)

    def dispatch(self, name: str, arguments_json: str) -> dict:
        if name not in self._name_to_skill:
            raise KeyError(f"no skill registered for tool name {name!r}")
        skill_id, version = self._name_to_skill[name]
        kwargs = json.loads(arguments_json) if arguments_json else {}
        return self._orchestrator.invoke(skill_id, version, kwargs)


def _name_map(skills: list[SkillSummary]) -> dict[str, tuple[str, str]]:
    return {s.id.replace("-", "_"): (s.id, s.version) for s in skills}


def build_openai_responses_toolset(orchestrator: SkillwardOrchestrator, query: str = "") -> OpenAIToolset:
    """Responses API shape: flat, `type`/`name`/`description`/`parameters` at
    the top level of each tool dict."""
    skills = orchestrator.discover(query)
    tools = [
        {
            "type": "function",
            "name": s.id.replace("-", "_"),
            "description": s.description,
            "parameters": s.input_schema,
        }
        for s in skills
    ]
    return OpenAIToolset(tools=tools, _orchestrator=orchestrator, _name_to_skill=_name_map(skills))


def build_openai_chat_completions_toolset(orchestrator: SkillwardOrchestrator, query: str = "") -> OpenAIToolset:
    """Chat Completions API shape: each tool dict wraps name/description/
    parameters under a nested "function" key."""
    skills = orchestrator.discover(query)
    tools = [
        {
            "type": "function",
            "function": {
                "name": s.id.replace("-", "_"),
                "description": s.description,
                "parameters": s.input_schema,
            },
        }
        for s in skills
    ]
    return OpenAIToolset(tools=tools, _orchestrator=orchestrator, _name_to_skill=_name_map(skills))
