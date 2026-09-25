"""Expose OSP skills, discovered from a registry at agent-build time, as
LangChain StructuredTools — this is the piece meant to be upstreamed."""

from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, ConfigDict, create_model

from .manifest import SkillSummary
from .orchestrator import HermesOrchestrator

_JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _schema_to_pydantic_model(name: str, schema: dict) -> type[BaseModel]:
    """Best-effort JSON-Schema -> pydantic model, enough for flat skill inputs."""
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    fields: dict[str, Any] = {}
    for prop_name, prop_schema in properties.items():
        py_type = _JSON_TYPE_MAP.get(prop_schema.get("type", "string"), str)
        default = ... if prop_name in required else prop_schema.get("default", None)
        fields[prop_name] = (py_type, default)
    if not fields:
        fields["_"] = (dict, {})
    return create_model(name, __config__=ConfigDict(extra="allow"), **fields)


def build_langchain_tool(orchestrator: HermesOrchestrator, skill: SkillSummary) -> StructuredTool:
    """Wrap a single discovered skill as a LangChain StructuredTool.

    The tool fetches, verifies, and executes the skill's payload on first
    call — nothing is downloaded until the agent actually decides to use it.
    """
    args_model = _schema_to_pydantic_model(f"{skill.id.replace('-', '_')}_args", skill.input_schema)

    def _call(**kwargs: Any) -> dict:
        return orchestrator.invoke(skill.id, skill.version, kwargs)

    return StructuredTool.from_function(
        func=_call,
        name=skill.id.replace("-", "_"),
        description=skill.description,
        args_schema=args_model,
    )


def build_langchain_tools(orchestrator: HermesOrchestrator, query: str = "") -> list[StructuredTool]:
    """Discover skills matching `query` in the registry and wrap each as a Tool."""
    return [build_langchain_tool(orchestrator, s) for s in orchestrator.discover(query)]
