# webpage-pdf-finder

Fetches a webpage and returns every PDF link found on it. This is the
fetch-only half of `examples/find_and_summarize_pdfs.py` — the *only* thing
that runs inside the sandbox for that example; everything downstream
(picking links, downloading PDFs, extracting text, summarizing) happens in
the orchestrator's own trusted code, never here.

## Manifest

| Field | Value |
|---|---|
| `runtime` | `python3.13` |
| `entrypoint` | `run.py:run` |
| `capabilities` | `net:<url-glob>` — scoped to just the host being fetched |

**Input**
```json
{"url": "https://example.com/reports/"}
```

**Output**
```json
{"pdf_links": ["https://example.com/reports/q3.pdf", "..."], "count": 1}
```

## How it works

Fetches `url` via `__net_fetch__` (granted by the `net:` capability), then
regex-matches `href="....pdf..."` attributes out of the HTML and resolves
each one to an absolute URL with `urljoin`. No HTML parser dependency —
`-S` isolated Python has no site-packages, so this sticks to stdlib
(`re`, `urllib.parse`).

## Publishing it yourself

```python
import httpx

httpx.post(
    f"{GATEWAY_URL}/skills/webpage-pdf-finder/1.0.0",
    headers={"Authorization": f"Bearer {api_key}"},
    json={
        "name": "Webpage PDF Finder",
        "description": "Fetches a webpage and returns every PDF link found on it",
        "runtime": "python3.13",
        "entrypoint": "run.py:run",
        "input_schema": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
        "output_schema": {
            "type": "object",
            "properties": {"pdf_links": {"type": "array", "items": {"type": "string"}}, "count": {"type": "integer"}},
            "required": ["pdf_links", "count"],
        },
        "capabilities": ["net:https://example.com/*"],  # scope to whatever host you're targeting
        "visibility": "public",
        "code": open("run.py").read(),
    },
)
```

See `examples/find_and_summarize_pdfs.py` for the full runnable version
(publishes this skill, invokes it, then does the rest in orchestrator code).
