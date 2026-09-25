"""Expose Skillward skills as LlamaIndex `FunctionTool`s."""

from __future__ import annotations

from typing import Any

from llama_index.core.tools import FunctionTool

from ._pydantic_schema import schema_to_pydantic_model
from .manifest import SkillSummary
from .orchestrator import SkillwardOrchestrator


def build_llamaindex_tool(orchestrator: SkillwardOrchestrator, skill: SkillSummary) -> FunctionTool:
    args_model = schema_to_pydantic_model(f"{skill.id.replace('-', '_')}_args", skill.input_schema)

    def _call(**kwargs: Any) -> dict:
        return orchestrator.invoke(skill.id, skill.version, kwargs)

    return FunctionTool.from_defaults(
        fn=_call,
        name=skill.id.replace("-", "_"),
        description=skill.description,
        fn_schema=args_model,
    )


def build_llamaindex_tools(orchestrator: SkillwardOrchestrator, query: str = "") -> list[FunctionTool]:
    """Discover skills matching `query` and wrap each as a LlamaIndex FunctionTool."""
    return [build_llamaindex_tool(orchestrator, s) for s in orchestrator.discover(query)]
