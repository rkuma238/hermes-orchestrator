"""Synthesizes a real Python callable with a genuine `inspect.Signature` from
a JSON Schema.

Some framework tool wrappers (AutoGen's `FunctionTool`, Google ADK's
`FunctionTool`) don't accept an explicit schema object the way LangChain's
`StructuredTool` or LlamaIndex's `FunctionTool` do — they generate the tool
schema the LLM sees by introspecting a real Python function's signature,
type hints, and docstring. Since a skill's input shape is only known at
discovery time (it comes from the registry, not from code we wrote), we
build a function object with a synthetic-but-real signature that matches the
skill's `input_schema`, so those frameworks' own introspection produces the
right result.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

_JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def build_function_from_schema(
    *,
    name: str,
    description: str,
    json_schema: dict,
    call: Callable[[dict], Any],
) -> Callable[..., Any]:
    """Returns a callable that looks, to `inspect`, like a real function with
    one parameter per `json_schema["properties"]` entry — required
    properties first (Python signatures require non-default parameters
    before defaulted ones, regardless of the schema's own property order).
    Invoking it calls `call(kwargs)` with whatever keyword arguments the
    caller supplied.
    """
    properties: dict = json_schema.get("properties", {})
    required = set(json_schema.get("required", []))
    ordered_props = sorted(properties.items(), key=lambda item: item[0] not in required)

    parameters = []
    annotations: dict[str, Any] = {}
    doc_lines = [description]
    if properties:
        doc_lines += ["", "Args:"]

    for prop_name, prop_schema in ordered_props:
        py_type = _JSON_TYPE_MAP.get(prop_schema.get("type", "string"), str)
        annotations[prop_name] = py_type
        is_required = prop_name in required
        parameters.append(
            inspect.Parameter(
                prop_name,
                kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=inspect.Parameter.empty if is_required else None,
                annotation=py_type,
            )
        )
        prop_desc = prop_schema.get("description", "")
        doc_lines.append(f"    {prop_name} ({py_type.__name__}): {prop_desc}")

    def _fn(**kwargs: Any) -> Any:
        return call(kwargs)

    _fn.__name__ = name
    _fn.__doc__ = "\n".join(doc_lines)
    _fn.__signature__ = inspect.Signature(parameters=parameters, return_annotation=dict)
    _fn.__annotations__ = {**annotations, "return": dict}
    return _fn
