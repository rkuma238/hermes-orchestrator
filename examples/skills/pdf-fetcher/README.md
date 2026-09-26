# pdf-fetcher

Fetches a PDF, extracts its text, and hands off to a summarizer skill via
`call_next`. This is hop 1 of the chain in both
`examples/chain_fetch_and_summarize_pdf.py` (hands off to `pdf-summarizer`)
and `examples/chain_fetch_and_summarize_pdf_combo.py` (hands off to
`pdf-summarizer-combo`) — which one is a caller choice, not baked in (see
"How it works" below). Unlike `webpage-pdf-finder` +
`find_and_summarize_pdfs.py` (where the orchestrator's own code does the
downloading/extracting), this skill puts the *entire* pipeline inside
skills, with the orchestrator following the hand-off between them itself.

## Manifest

| Field | Value |
|---|---|
| `runtime` | `python3.13` |
| `entrypoint` | `run.py:run` |
| `capabilities` | `net:<pdf-host-glob>`, `skill:pdf-summarizer`, `skill:pdf-summarizer-combo` — both hand-off targets declared; each script's own `allowed_capabilities` decides which is actually usable for that run |
| `resource_limits.timeout_seconds` | `45` — generous, since the shared chain deadline (see spec/SPEC.md's "Chain calls") is computed from *this* hop's own budget and has to cover hop 2's OpenRouter round trip as well |

**Input**
```json
{"url": "https://example.com/report.pdf", "next_skill_id": "pdf-summarizer", "system_prompt": "(optional)"}
```
`next_skill_id` defaults to `"pdf-summarizer"` if omitted. `system_prompt` is
only meaningful when handing off to `pdf-summarizer` — `pdf-summarizer-combo`
carries its own prompt bundled in, so it ignores it.

**Output** (always a hand-off — this skill never returns a "real" result)
```json
{"call_next": {"id": "pdf-summarizer", "version": "1.0.0", "input": {"text": "...", "source_url": "..."}}}
```

## How it works

1. Fetches `url` via `__net_fetch__` and reads `resp["body_base64"]` — **not**
   `resp["body"]`, which is utf-8-decoded and would corrupt a PDF's binary
   bytes. Decodes the base64 back to raw bytes.
2. Finds every `stream ... endstream` block in the raw PDF bytes and tries
   `zlib.decompress` on each — most PDF content streams (the parts holding
   visible text and drawing commands) use Flate/zlib compression.
3. Within each successfully decompressed stream, regex-matches parenthesized
   string literals (`(...)`, handling `\(`, `\)`, `\\` escapes) — in an
   ordinary content stream, these are almost always arguments to the
   `Tj`/`TJ` text-show operators, so grabbing all of them in order recovers
   most of the document's visible text without needing a real PDF parser
   (not available inside the sandbox — no pip installs, no site-packages).
4. Joins and whitespace-normalizes the extracted text, caps it at 20,000
   characters, and hands off to `pdf-summarizer` with it.

This is a naive extractor — no font/encoding tables, no reading-order
awareness beyond stream order — but works reasonably well on ordinary
text-heavy PDFs (press releases, filings, reports). It will do worse on
image-heavy, scanned, or complex multi-column layouts.

See `examples/chain_fetch_and_summarize_pdf.py` for the full runnable chain.
