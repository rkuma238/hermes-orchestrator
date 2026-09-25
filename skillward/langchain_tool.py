"""Expose Skillward skills, discovered from a registry at agent-build time, as
LangChain StructuredTools — this is the piece meant to be upstreamed."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from ._pydantic_schema import schema_to_pydantic_model
from .manifest import SkillSummary
from .orchestrator import SkillwardOrchestrator


def build_langchain_tool(orchestrator: SkillwardOrchestrator, skill: SkillSummary) -> StructuredTool:
    """Wrap a single discovered skill as a LangChain StructuredTool.

    The tool fetches, verifies, and executes the skill's payload on first
    call — nothing is downloaded until the agent actually decides to use it.
    """
    args_model = schema_to_pydantic_model(f"{skill.id.replace('-', '_')}_args", skill.input_schema)

    def _call(**kwargs: Any) -> dict:
        return orchestrator.invoke(skill.id, skill.version, kwargs)

    return StructuredTool.from_function(
        func=_call,
        name=skill.id.replace("-", "_"),
        description=skill.description,
        args_schema=args_model,
    )


def build_langchain_tools(orchestrator: SkillwardOrchestrator, query: str = "") -> list[StructuredTool]:
    """Discover skills matching `query` in the registry and wrap each as a Tool."""
    return [build_langchain_tool(orchestrator, s) for s in orchestrator.discover(query)]
