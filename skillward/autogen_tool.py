"""Expose Skillward skills as Microsoft AutoGen `FunctionTool`s
(autogen_core.tools). AutoGen generates a tool's schema by introspecting a
real Python function's type hints, not from an explicit schema object, so
each skill is wrapped via `_dynamic_function.build_function_from_schema`
rather than a hand-written function.
"""

from __future__ import annotations

from autogen_core.tools import FunctionTool

from ._dynamic_function import build_function_from_schema
from .manifest import SkillSummary
from .orchestrator import SkillwardOrchestrator


def build_autogen_tool(orchestrator: SkillwardOrchestrator, skill: SkillSummary) -> FunctionTool:
    fn = build_function_from_schema(
        name=skill.id.replace("-", "_"),
        description=skill.description,
        json_schema=skill.input_schema,
        call=lambda kwargs: orchestrator.invoke(skill.id, skill.version, kwargs),
    )
    return FunctionTool(fn, description=skill.description, name=skill.id.replace("-", "_"))


def build_autogen_tools(orchestrator: SkillwardOrchestrator, query: str = "") -> list[FunctionTool]:
    """Discover skills matching `query` and wrap each as an AutoGen FunctionTool."""
    return [build_autogen_tool(orchestrator, s) for s in orchestrator.discover(query)]
