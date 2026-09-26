"""One skill fetches; the orchestrator does everything else.

A single Skillward skill (published by this script) fetches a page through
the real registry + gateway + sandbox stack, using the `net:` capability's
`__net_fetch__`, and returns every PDF link found on it — that's the only
thing that runs inside the sandbox. Everything downstream — picking the
most relevant links, downloading the actual PDFs, extracting their text,
and summarizing each with an LLM — happens in this script's own trusted
code, never inside the sandbox. That split is deliberate: an LLM API key
and raw PDF bytes should never have to enter untrusted skill code (see
README.md's "Network access" section and spec/SPEC.md's "5b. Network
access" for why `net:` isn't a hard boundary for Python skills).

Prereqs:
    uvicorn registry_server.main:app --port 8079
    uvicorn partner_service.main:app --port 8082
    uvicorn labs_service.main:app --port 8083
    envoy -c envoy/envoy.yaml
    brew install poppler   # provides pdftotext, used for text extraction
                            # (apt-get install poppler-utils on Linux)

Summarization is optional — omit OPENROUTER_API_KEY and this just lists the
PDF links the skill found. To enable it:
    export OPENROUTER_API_KEY=sk-or-...   # https://openrouter.ai/keys

Run:
    python -m examples.find_and_summarize_pdfs https://example.com/reports/
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import httpx

from skillward import SkillwardOrchestrator

GATEWAY_URL = "http://127.0.0.1:10000"
TOP_N = 2
SUMMARY_MODEL = "google/gemini-2.5-flash"
FETCH_SKILL_ID = "webpage-pdf-finder"
# The actual skill source, as a real file: see examples/skills/webpage-pdf-finder/README.md
FETCH_SKILL_CODE = (Path(__file__).parent / "skills" / FETCH_SKILL_ID / "run.py").read_text()


def publish_fetch_skill(gateway_url: str, api_key: str, net_pattern: str) -> None:
    # Public and already published (e.g. by an earlier run, possibly by a
    # different account) is fine to reuse as-is — any account can invoke a
    # public skill; only republishing/modifying it requires ownership.
    existing = httpx.get(
        f"{gateway_url}/skills/{FETCH_SKILL_ID}/1.0.0/manifest",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    if existing.status_code == 200:
        return

    resp = httpx.post(
        f"{gateway_url}/skills/{FETCH_SKILL_ID}/1.0.0",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "name": "Webpage PDF Finder",
            "description": "Fetches a webpage and returns every PDF link found on it",
            "runtime": "python3.13",
            "entrypoint": "run.py:run",
            "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
            "output_schema": {
                "type": "object",
                "properties": {
                    "pdf_links": {"type": "array", "items": {"type": "string"}},
                    "count": {"type": "integer"},
                },
                "required": ["pdf_links", "count"],
            },
            "capabilities": [f"net:{net_pattern}"],
            "resource_limits": {"timeout_seconds": 20, "max_memory_mb": 256},
            "visibility": "public",
            "code": FETCH_SKILL_CODE,
        },
    )
    resp.raise_for_status()


def pick_top_links(pdf_links: list[str], n: int) -> list[str]:
    """Prefers one link per distinct source domain — a single filing
    repeated across dozens of URLs on the same host is less useful for a
    summary than one document per source."""
    seen_domains: set[str] = set()
    picks: list[str] = []
    for link in pdf_links:
        domain = urllib.parse.urlparse(link).netloc
        if domain in seen_domains:
            continue
        seen_domains.add(domain)
        picks.append(link)
        if len(picks) == n:
            break
    return picks


def download_pdf(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; SkillwardBot/1.0)"})
    with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as f:
        f.write(resp.read())


def extract_text(pdf_path: Path, max_chars: int) -> str:
    if not shutil.which("pdftotext"):
        raise RuntimeError(
            "pdftotext not found — install poppler (`brew install poppler` or "
            "`apt-get install poppler-utils`) to enable text extraction"
        )
    proc = subprocess.run(["pdftotext", "-layout", str(pdf_path), "-"], capture_output=True, text=True, timeout=30)
    text = re.sub(r"\n{3,}", "\n\n", proc.stdout).strip()
    return text[:max_chars]


def summarize_with_llm(api_key: str, label: str, text: str) -> str:
    payload = {
        "model": SUMMARY_MODEL,
        "max_tokens": 400,
        "messages": [
            {
                "role": "system",
                "content": "You are a concise analyst. Summarize the given document in 5-6 crisp bullet "
                "points, suitable for a busy reader. Only use facts present in the text.",
            },
            {"role": "user", "content": f"Summarize this document ({label}):\n\n{text}"},
        ],
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            body = json.loads(resp.read())
        return body["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as e:
        return f"[summarization failed: HTTP {e.code} — {e.read().decode()[:300]}]"


def main():
    if len(sys.argv) != 2:
        print("usage: python -m examples.find_and_summarize_pdfs <url>")
        raise SystemExit(1)
    target_url = sys.argv[1]
    net_pattern = f"https://{urllib.parse.urlparse(target_url).netloc}/*"

    openrouter_key = os.environ.get("OPENROUTER_API_KEY")
    if not openrouter_key:
        print("OPENROUTER_API_KEY not set — will list PDF links only, skipping summarization.\n")

    print("== sign up ==")
    resp = httpx.post(f"{GATEWAY_URL}/accounts", json={"name": "pdf-finder-example"})
    resp.raise_for_status()
    account = resp.json()

    print(f"== publish {FETCH_SKILL_ID}@1.0.0 (the one fetch skill) ==")
    publish_fetch_skill(GATEWAY_URL, account["api_key"], net_pattern)

    print(f"== orchestrator invokes the skill against {target_url} ==")
    with SkillwardOrchestrator(
        GATEWAY_URL, api_key=account["api_key"], allowed_capabilities={f"net:{net_pattern}"}
    ) as orch:
        result = orch.invoke(FETCH_SKILL_ID, "1.0.0", {"url": target_url})

    print(f"  skill found {result['count']} PDF link(s)")
    for link in result["pdf_links"]:
        print(f"   - {link}")

    if not openrouter_key or not result["pdf_links"]:
        return

    top_links = pick_top_links(result["pdf_links"], TOP_N)
    print(f"\n== orchestrator picks top {len(top_links)} (by distinct source) and takes over from here ==")

    with tempfile.TemporaryDirectory(prefix="skillward-pdf-example-") as tmp:
        for i, url in enumerate(top_links, start=1):
            print(f"\n== [{i}/{len(top_links)}] orchestrator downloads + extracts + summarizes ==")
            print(f"  url: {url}")
            pdf_path = Path(tmp) / f"doc_{i}.pdf"
            download_pdf(url, pdf_path)
            print(f"  downloaded {pdf_path.stat().st_size:,} bytes")
            text = extract_text(pdf_path, max_chars=12000)
            print(f"  extracted {len(text):,} chars of text")
            summary = summarize_with_llm(openrouter_key, url, text)
            print(f"\n  --- summary ({url}) ---")
            print("  " + summary.replace("\n", "\n  "))


if __name__ == "__main__":
    main()
