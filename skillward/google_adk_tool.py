"""Expose Skillward skills as Google ADK `FunctionTool`s.

Like AutoGen, ADK generates a tool's schema by introspecting a real Python
function's type hints and (Google-style) docstring rather than accepting an
explicit schema object, so each skill is wrapped via
`_dynamic_function.build_function_from_schema`.
"""

from __future__ import annotations

from google.adk.tools import FunctionTool

from ._dynamic_function import build_function_from_schema
from .manifest import SkillSummary
from .orchestrator import SkillwardOrchestrator


def build_google_adk_tool(orchestrator: SkillwardOrchestrator, skill: SkillSummary) -> FunctionTool:
    fn = build_function_from_schema(
        name=skill.id.replace("-", "_"),
        description=skill.description,
        json_schema=skill.input_schema,
        call=lambda kwargs: orchestrator.invoke(skill.id, skill.version, kwargs),
    )
    return FunctionTool(fn)


def build_google_adk_tools(orchestrator: SkillwardOrchestrator, query: str = "") -> list[FunctionTool]:
    """Discover skills matching `query` and wrap each as a Google ADK FunctionTool."""
    return [build_google_adk_tool(orchestrator, s) for s in orchestrator.discover(query)]
