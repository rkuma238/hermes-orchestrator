"""Two skills, chained via call_next, entirely inside the protocol.

Unlike examples/find_and_summarize_pdfs.py (where the orchestrator's own
trusted code does the downloading/extracting/summarizing after a single
fetch skill runs), this example expresses the *whole* pipeline as two
skills, with the orchestrator following the hand-off between them itself:

  1. pdf-fetcher: fetches a PDF via __net_fetch__ (using body_base64 for
     byte-exact binary fidelity — body alone would corrupt it), extracts
     its text with a small dependency-free PDF text extractor (stdlib
     zlib + regex over content streams — no pypdf/pdfplumber available
     inside the sandbox), and hands off to pdf-summarizer via call_next.
  2. pdf-summarizer: takes the extracted text, calls Gemini via OpenRouter
     using an env:-granted API key, and returns the summary as the final
     result.

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

import httpx

from skillward import SkillwardOrchestrator

GATEWAY_URL = "http://127.0.0.1:10000"
OPENROUTER_PATTERN = "https://openrouter.ai/*"
SUMMARY_MODEL = "google/gemini-2.5-flash"

FETCHER_CODE = r"""
def run(input_data):
    import base64, re, zlib

    url = input_data["url"]
    resp = __net_fetch__(url, headers={"User-Agent": "Mozilla/5.0 (compatible; SkillwardBot/1.0)"})
    pdf_bytes = base64.b64decode(resp["body_base64"])

    # Minimal, dependency-free PDF text extraction: most PDF content streams
    # are zlib-compressed; once decompressed, visible text mostly shows up
    # as parenthesized string literals passed to the Tj/TJ show-text
    # operators. This is a naive extractor (no font/encoding awareness) but
    # works reasonably on ordinary text-heavy PDFs.
    text_parts = []
    for stream in re.findall(rb"stream\r?\n(.*?)endstream", pdf_bytes, re.DOTALL):
        try:
            decompressed = zlib.decompress(stream)
        except Exception:
            continue
        for literal in re.findall(rb"\((?:[^()\\]|\\.)*\)", decompressed):
            inner = literal[1:-1].replace(rb"\(", b"(").replace(rb"\)", b")").replace(rb"\\", b"\\")
            try:
                text_parts.append(inner.decode("latin-1"))
            except Exception:
                pass

    text = re.sub(r"\s+", " ", " ".join(text_parts)).strip()
    if not text:
        raise ValueError("no extractable text found in PDF")

    return {
        "call_next": {
            "id": "pdf-summarizer",
            "version": "1.0.0",
            "input": {"text": text[:20000], "source_url": url},
        }
    }
"""

SUMMARIZER_CODE = r"""
def run(input_data):
    import json, os

    text = input_data["text"]
    source_url = input_data.get("source_url", "")
    api_key = os.environ["OPENROUTER_API_KEY"]

    payload = json.dumps({
        "model": "google/gemini-2.5-flash",
        "max_tokens": 400,
        "messages": [
            {
                "role": "system",
                "content": "You are a concise analyst. Summarize the given document in 5-6 crisp bullet "
                           "points, suitable for a busy reader. Only use facts present in the text.",
            },
            {"role": "user", "content": "Summarize this document (" + source_url + "):\n\n" + text},
        ],
    })
    resp = __net_fetch__(
        "https://openrouter.ai/api/v1/chat/completions",
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        body=payload,
    )
    body = json.loads(resp["body"])
    if "choices" not in body:
        raise RuntimeError("openrouter error: " + json.dumps(body)[:500])
    return {"summary": body["choices"][0]["message"]["content"], "source_url": source_url}
"""


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

    print("== publish pdf-fetcher@1.0.0 (hop 1: fetch + extract, hands off) ==")
    publish_if_missing(
        GATEWAY_URL,
        account["api_key"],
        "pdf-fetcher",
        {
            "name": "PDF Fetcher",
            "description": "Fetches a PDF and hands its extracted text off to pdf-summarizer",
            "runtime": "python3.13",
            "entrypoint": "run.py:run",
            "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
            "output_schema": {"type": "object"},
            "capabilities": [f"net:{pdf_host_pattern}", "skill:pdf-summarizer"],
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
            "code": SUMMARIZER_CODE,
        },
    )

    allowed_capabilities = {
        f"net:{pdf_host_pattern}",
        "skill:pdf-summarizer",
        f"net:{OPENROUTER_PATTERN}",
        "env:OPENROUTER_API_KEY",
    }

    print(f"\n== invoking pdf-fetcher against {pdf_url} ==")
    print("   (the orchestrator follows the call_next hand-off to pdf-summarizer itself)")
    with SkillwardOrchestrator(
        GATEWAY_URL, api_key=account["api_key"], allowed_capabilities=allowed_capabilities
    ) as orch:
        result = orch.invoke("pdf-fetcher", "1.0.0", {"url": pdf_url})

    print("\n== final result (returned by hop 2, pdf-summarizer) ==")
    print(f"  source_url: {result['source_url']}")
    print(f"  summary:\n  {result['summary'].replace(chr(10), chr(10) + '  ')}")


if __name__ == "__main__":
    main()
