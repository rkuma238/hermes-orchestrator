"""One real discover+invoke test per framework adapter, against the shared
stack from conftest.py.

Each test skips (not fails) if that framework isn't installed — several of
these frameworks conflict with each other in the same environment (crewai
and google-adk pull incompatible protobuf versions as of when this was
written), so this suite is meant to be run against whichever single extra
is installed, not all of them at once. CI runs each in its own job/venv;
see .github/workflows/ci.yml.
"""

import json

import pytest

from skillward.openai_tool import build_openai_chat_completions_toolset, build_openai_responses_toolset


def test_openai_responses_toolset(orchestrator):
    toolset = build_openai_responses_toolset(orchestrator)
    tool = next(t for t in toolset.tools if t["name"] == "labs_reverse_text")
    assert tool["type"] == "function"
    assert tool["parameters"]["required"] == ["text"]
    result = toolset.dispatch("labs_reverse_text", json.dumps({"text": "responses"}))
    assert result == {"reversed": "sesnopser"}


def test_openai_chat_completions_toolset(orchestrator):
    toolset = build_openai_chat_completions_toolset(orchestrator)
    tool = next(t for t in toolset.tools if t["function"]["name"] == "example_word_count")
    assert tool["type"] == "function"
    result = toolset.dispatch("example_word_count", json.dumps({"text": "one two three"}))
    assert result == {"word_count": 3, "char_count": 13}


def test_langchain_tool(orchestrator):
    pytest.importorskip("langchain_core")
    from skillward.langchain_tool import build_langchain_tools

    tools = build_langchain_tools(orchestrator)
    echo_tool = next(t for t in tools if t.name == "example_echo")
    result = echo_tool.invoke({"message": "langchain"})
    assert result == {"echo": "langchain", "length": 9}


def test_llamaindex_tool(orchestrator):
    pytest.importorskip("llama_index.core.tools")
    from skillward.llamaindex_tool import build_llamaindex_tools

    tools = build_llamaindex_tools(orchestrator)
    reverse_tool = next(t for t in tools if t.metadata.name == "labs_reverse_text")
    result = reverse_tool.call(text="llamaindex")
    assert result.raw_output == {"reversed": "xedniamall"}


def test_crewai_tool(orchestrator):
    pytest.importorskip("crewai")
    from skillward.crewai_tool import build_crewai_tools

    tools = build_crewai_tools(orchestrator)
    reverse_tool = next(t for t in tools if t.name == "labs_reverse_text")
    result = reverse_tool.run(text="crewai")
    assert result == {"reversed": "iawerc"}


def test_autogen_tool(orchestrator):
    autogen_core = pytest.importorskip("autogen_core")
    pytest.importorskip("autogen_core.tools")
    from skillward.autogen_tool import build_autogen_tools

    tools = build_autogen_tools(orchestrator)
    reverse_tool = next(t for t in tools if t.name == "labs_reverse_text")

    import asyncio

    result = asyncio.run(reverse_tool.run_json({"text": "autogen"}, autogen_core.CancellationToken()))
    assert result == {"reversed": "negotua"}


def test_google_adk_tool(orchestrator):
    pytest.importorskip("google.adk.tools")
    from skillward.google_adk_tool import build_google_adk_tools

    tools = build_google_adk_tools(orchestrator)
    reverse_tool = next(t for t in tools if t.name == "labs_reverse_text")

    import asyncio

    result = asyncio.run(reverse_tool.run_async(args={"text": "adk"}, tool_context=None))
    assert result == {"reversed": "kda"}
