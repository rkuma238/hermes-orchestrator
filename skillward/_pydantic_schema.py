"""Best-effort JSON Schema -> pydantic model conversion, shared by every
framework adapter that accepts an explicit schema object for a tool's
arguments (LangChain's `args_schema`, LlamaIndex's `fn_schema`, CrewAI's
`args_schema`). Good enough for flat skill inputs, which is all v0.1 skill
manifests describe.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, create_model

JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def schema_to_pydantic_model(name: str, schema: dict) -> type[BaseModel]:
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    fields: dict[str, Any] = {}
    for prop_name, prop_schema in properties.items():
        py_type = JSON_TYPE_MAP.get(prop_schema.get("type", "string"), str)
        default = ... if prop_name in required else prop_schema.get("default", None)
        fields[prop_name] = (py_type, default)
    if not fields:
        fields["_"] = (dict, {})
    return create_model(name, __config__=ConfigDict(extra="allow"), **fields)
