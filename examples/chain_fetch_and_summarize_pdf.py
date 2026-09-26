"""Two skills, chained via call_next, entirely inside the protocol — plus a
third, `text` runtime skill supplying the instructions for the LLM step.

Unlike examples/find_and_summarize_pdfs.py (where the orchestrator's own
trusted code does the downloading/extracting/summarizing after a single
fetch skill runs), this example expresses the *whole* pipeline as chained
skills, with the orchestrator following the hand-off between them itself:

  1. financial-summary-prompt: a `text` skill (not code — see
     examples/skills/financial-summary-prompt/README.md). Fetched once, up
     front, not as part of the chain — its content is plain prose, not the
     reserved call_next JSON shape (see sandbox.py's _run_text), so it always
     returns itself verbatim rather than handing off. Its text becomes the
     system prompt for step 3, steering the summary toward
     Revenue/EBITDA/Profit instead of a generic instruction.
  2. pdf-fetcher: fetches a PDF via __net_fetch__ (using body_base64 for
     byte-exact binary fidelity — body alone would corrupt it), extracts
     its text with a small dependency-free PDF text extractor (stdlib
     zlib + regex over content streams — no pypdf/pdfplumber available
     inside the sandbox), and hands off to pdf-summarizer via call_next,
     threading the prompt from step 1 straight through.
  3. pdf-summarizer: takes the extracted text and the prompt, calls Gemini
     via OpenRouter using an env:-granted API key, and returns the summary
     as the final result.

Trade-off worth being explicit about: this puts the OpenRouter API key and
the PDF's raw bytes inside sandboxed skill code, which examples/
find_and_summarize_pdfs.py deliberately avoids. Reach for that version by
default; this one exists to demonstrate a real two-hop call_next chain
doing genuine, non-trivial work at each step.

Prereqs:
    uvicorn registry_server.main:app --port 8079
    uvicorn partner_service.main:app --port 8082
    uvicorn labs_service.main:app --port 8083
    envoy -c envoy/envoy.yaml
    export OPENROUTER_API_KEY=sk-or-...   # https://openrouter.ai/keys

Run:
    python -m examples.chain_fetch_and_summarize_pdf <pdf-url>
"""

import os
import sys
import urllib.parse
from pathlib import Path

import httpx

from skillward import SkillwardOrchestrator

GATEWAY_URL = "http://127.0.0.1:10000"
OPENROUTER_PATTERN = "https://openrouter.ai/*"
SUMMARY_MODEL = "google/gemini-2.5-flash"

_SKILLS_DIR = Path(__file__).parent / "skills"
# The actual skill sources, as real files: see examples/skills/pdf-fetcher/,
# examples/skills/pdf-summarizer/, and examples/skills/financial-summary-prompt/
# for what each one does and why.
FETCHER_CODE = (_SKILLS_DIR / "pdf-fetcher" / "run.py").read_text()
SUMMARIZER_CODE = (_SKILLS_DIR / "pdf-summarizer" / "run.py").read_text()
PROMPT_CODE = (_SKILLS_DIR / "financial-summary-prompt" / "skill.md").read_text()


def publish_if_missing(gateway_url: str, api_key: str, skill_id: str, body: dict) -> None:
    # Public and already published (e.g. by an earlier run, possibly by a
    # different account) is fine to reuse as-is — any account can invoke a
    # public skill; only republishing/modifying it requires ownership.
    existing = httpx.get(
        f"{gateway_url}/skills/{skill_id}/1.0.0/manifest", headers={"Authorization": f"Bearer {api_key}"}
    )
    if existing.status_code == 200:
        return
    resp = httpx.post(
        f"{gateway_url}/skills/{skill_id}/1.0.0", headers={"Authorization": f"Bearer {api_key}"}, json=body
    )
    resp.raise_for_status()


def main():
    if len(sys.argv) != 2:
        print("usage: python -m examples.chain_fetch_and_summarize_pdf <pdf-url>")
        raise SystemExit(1)
    pdf_url = sys.argv[1]
    pdf_host_pattern = f"https://{urllib.parse.urlparse(pdf_url).netloc}/*"

    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    if not openrouter_key:
        print("OPENROUTER_API_KEY must be set for this example (https://openrouter.ai/keys)")
        raise SystemExit(1)

    print("== sign up ==")
    resp = httpx.post(f"{GATEWAY_URL}/accounts", json={"name": "chain-pdf-summary-example"})
    resp.raise_for_status()
    account = resp.json()

    print("== publish financial-summary-prompt@1.0.0 (a text skill — not code) ==")
    publish_if_missing(
        GATEWAY_URL,
        account["api_key"],
        "financial-summary-prompt",
        {
            "name": "Financial Summary Prompt",
            "description": "Instructions steering a summary toward Revenue/EBITDA/Profit",
            "runtime": "text",
            "entrypoint": "skill.md",
            "input_schema": {"type": "object"},
            "output_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            "visibility": "public",
            "code": PROMPT_CODE,
        },
    )

    print("== publish pdf-fetcher@1.0.0 (hop 1: fetch + extract, hands off) ==")
    publish_if_missing(
        GATEWAY_URL,
        account["api_key"],
        "pdf-fetcher",
        {
            "name": "PDF Fetcher",
            "description": "Fetches a PDF and hands its extracted text off to a summarizer skill",
            "runtime": "python3.13",
            "entrypoint": "run.py:run",
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "next_skill_id": {"type": "string"},
                    "system_prompt": {"type": "string"},
                },
                "required": ["url"],
            },
            "output_schema": {"type": "object"},
            # Both hand-off targets declared; allowed_capabilities below is
            # what actually decides which is usable in this run.
            "capabilities": [f"net:{pdf_host_pattern}", "skill:pdf-summarizer", "skill:pdf-summarizer-combo"],
            "resource_limits": {"timeout_seconds": 45, "max_memory_mb": 256},
            "visibility": "public",
            "code": FETCHER_CODE,
        },
    )

    print("== publish pdf-summarizer@1.0.0 (hop 2: summarize via Gemini/OpenRouter, final result) ==")
    publish_if_missing(
        GATEWAY_URL,
        account["api_key"],
        "pdf-summarizer",
        {
            "name": "PDF Summarizer",
            "description": "Summarizes extracted PDF text via Gemini through OpenRouter",
            "runtime": "python3.13",
            "entrypoint": "run.py:run",
            "input_schema": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "source_url": {"type": "string"},
                    "system_prompt": {"type": "string"},
                },
                "required": ["text"],
            },
            "output_schema": {
                "type": "object",
                "properties": {"summary": {"type": "string"}, "source_url": {"type": "string"}},
                "required": ["summary"],
            },
            "capabilities": [f"net:{OPENROUTER_PATTERN}", "env:OPENROUTER_API_KEY"],
            "resource_limits": {"timeout_seconds": 30, "max_memory_mb": 256},
            "visibility": "public",
            "code": SUMMARIZER_CODE,
        },
    )

    allowed_capabilities = {
        f"net:{pdf_host_pattern}",
        # Both granted even though this run only exercises one: every
        # capability pdf-fetcher's manifest *declares* must be granted for
        # it to run at all, whether or not this particular call uses it.
        "skill:pdf-summarizer",
        "skill:pdf-summarizer-combo",
        f"net:{OPENROUTER_PATTERN}",
        "env:OPENROUTER_API_KEY",
    }

    with SkillwardOrchestrator(
        GATEWAY_URL, api_key=account["api_key"], allowed_capabilities=allowed_capabilities
    ) as orch:
        print("\n== fetching financial-summary-prompt (not chained — a text skill can't call_next) ==")
        system_prompt = orch.invoke("financial-summary-prompt", "1.0.0", {})["text"]
        print(f"  got {len(system_prompt)} chars of instructions")

        print(f"\n== invoking pdf-fetcher against {pdf_url} ==")
        print("   (the orchestrator follows the call_next hand-off to pdf-summarizer itself)")
        result = orch.invoke("pdf-fetcher", "1.0.0", {"url": pdf_url, "system_prompt": system_prompt})

    print("\n== final result (returned by hop 2, pdf-summarizer) ==")
    print(f"  source_url: {result['source_url']}")
    print(f"  summary:\n  {result['summary'].replace(chr(10), chr(10) + '  ')}")


if __name__ == "__main__":
    main()
