"""Expose Skillward skills as CrewAI `BaseTool`s.

CrewAI tools are pydantic-model classes (name/description/args_schema as
class attributes, plus a `_run` method), not factory-built objects the way
LangChain/LlamaIndex tools are — so each skill gets its own dynamically
created `BaseTool` subclass.
"""

from __future__ import annotations

from typing import Any

from crewai.tools import BaseTool

from ._pydantic_schema import schema_to_pydantic_model
from .manifest import SkillSummary
from .orchestrator import SkillwardOrchestrator


def build_crewai_tool(orchestrator: SkillwardOrchestrator, skill: SkillSummary) -> BaseTool:
    args_model = schema_to_pydantic_model(f"{skill.id.replace('-', '_')}_args", skill.input_schema)

    def _run(self: BaseTool, **kwargs: Any) -> dict:
        return orchestrator.invoke(skill.id, skill.version, kwargs)

    tool_cls = type(
        f"{skill.id.replace('-', '_')}_tool",
        (BaseTool,),
        {
            "__module__": __name__,
            "__annotations__": {"name": str, "description": str, "args_schema": type[args_model]},
            "name": skill.id.replace("-", "_"),
            "description": skill.description,
            "args_schema": args_model,
            "_run": _run,
        },
    )
    return tool_cls()


def build_crewai_tools(orchestrator: SkillwardOrchestrator, query: str = "") -> list[BaseTool]:
    """Discover skills matching `query` and wrap each as a CrewAI BaseTool."""
    return [build_crewai_tool(orchestrator, s) for s in orchestrator.discover(query)]
