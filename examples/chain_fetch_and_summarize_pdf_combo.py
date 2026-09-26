"""The same chain as examples/chain_fetch_and_summarize_pdf.py, but with the
prompt bundled directly into the summarizer skill instead of fetched
separately — see examples/skills/pdf-summarizer-combo/README.md for the
trade-off between the two approaches.

  1. pdf-fetcher: fetches the PDF, extracts text, hands off via call_next —
     told to target pdf-summarizer-combo via "next_skill_id" in its input
     (see examples/skills/pdf-fetcher/README.md).
  2. pdf-summarizer-combo: a *combo* skill — published with `files`, not
     `code` — bundling run.py together with a companion prompt.md. Reads
     its own prompt via __bundle__["prompt.md"] (see spec/SPEC.md's "Combo
     skills"), calls Gemini via OpenRouter, and returns the summary.

No separate text-skill fetch step this time: the prompt travels with the
code as one versioned unit rather than being a second thing the caller has
to know to fetch and thread through.

Prereqs:
    uvicorn registry_server.main:app --port 8079
    uvicorn partner_service.main:app --port 8082
    uvicorn labs_service.main:app --port 8083
    envoy -c envoy/envoy.yaml
    export OPENROUTER_API_KEY=sk-or-...   # https://openrouter.ai/keys

Run:
    python -m examples.chain_fetch_and_summarize_pdf_combo <pdf-url>
"""

import os
import sys
import urllib.parse
from pathlib import Path

import httpx

from skillward import SkillwardOrchestrator

GATEWAY_URL = "http://127.0.0.1:10000"
OPENROUTER_PATTERN = "https://openrouter.ai/*"

_SKILLS_DIR = Path(__file__).parent / "skills"
FETCHER_CODE = (_SKILLS_DIR / "pdf-fetcher" / "run.py").read_text()
COMBO_RUN_CODE = (_SKILLS_DIR / "pdf-summarizer-combo" / "run.py").read_text()
COMBO_PROMPT = (_SKILLS_DIR / "pdf-summarizer-combo" / "prompt.md").read_text()


def publish_if_missing(gateway_url: str, api_key: str, skill_id: str, body: dict) -> None:
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
        print("usage: python -m examples.chain_fetch_and_summarize_pdf_combo <pdf-url>")
        raise SystemExit(1)
    pdf_url = sys.argv[1]
    pdf_host_pattern = f"https://{urllib.parse.urlparse(pdf_url).netloc}/*"

    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    if not openrouter_key:
        print("OPENROUTER_API_KEY must be set for this example (https://openrouter.ai/keys)")
        raise SystemExit(1)

    print("== sign up ==")
    resp = httpx.post(f"{GATEWAY_URL}/accounts", json={"name": "chain-pdf-summary-combo-example"})
    resp.raise_for_status()
    account = resp.json()

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
            "capabilities": [f"net:{pdf_host_pattern}", "skill:pdf-summarizer", "skill:pdf-summarizer-combo"],
            "resource_limits": {"timeout_seconds": 45, "max_memory_mb": 256},
            "visibility": "public",
            "code": FETCHER_CODE,
        },
    )

    print("== publish pdf-summarizer-combo@1.0.0 (a combo skill: code + bundled prompt.md) ==")
    publish_if_missing(
        GATEWAY_URL,
        account["api_key"],
        "pdf-summarizer-combo",
        {
            "name": "PDF Summarizer (combo)",
            "description": "Summarizes extracted PDF text via Gemini, with its prompt bundled in",
            "runtime": "python3.13",
            "entrypoint": "run.py:run",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "source_url": {"type": "string"}},
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
            # `files`, not `code` — see spec/SPEC.md's "Combo skills".
            "files": {"run.py": COMBO_RUN_CODE, "prompt.md": COMBO_PROMPT},
        },
    )

    allowed_capabilities = {
        f"net:{pdf_host_pattern}",
        "skill:pdf-summarizer",
        "skill:pdf-summarizer-combo",
        f"net:{OPENROUTER_PATTERN}",
        "env:OPENROUTER_API_KEY",
    }

    print(f"\n== invoking pdf-fetcher against {pdf_url} ==")
    print("   (told to hand off to pdf-summarizer-combo, which carries its own prompt)")
    with SkillwardOrchestrator(
        GATEWAY_URL, api_key=account["api_key"], allowed_capabilities=allowed_capabilities
    ) as orch:
        result = orch.invoke("pdf-fetcher", "1.0.0", {"url": pdf_url, "next_skill_id": "pdf-summarizer-combo"})

    print("\n== final result (returned by hop 2, pdf-summarizer-combo) ==")
    print(f"  source_url: {result['source_url']}")
    print(f"  summary:\n  {result['summary'].replace(chr(10), chr(10) + '  ')}")


if __name__ == "__main__":
    main()
