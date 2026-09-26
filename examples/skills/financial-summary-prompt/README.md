# financial-summary-prompt

A **`text` runtime** skill — not code. Its payload (`skill.md`) is a prompt
that steers a summary toward specific financial line items (Revenue,
EBITDA, Profit, and other explicitly-stated ratios) instead of a generic
"summarize this" instruction. Invoking it runs no code and touches no
network; it just returns its own content verbatim.

## Manifest

| Field | Value |
|---|---|
| `runtime` | `text` |
| `entrypoint` | `skill.md` — a bare filename, no function (text skills have nothing to call) |
| `capabilities` | none needed |

**Input**: `{}` (ignored — a text skill's result never depends on its input)

**Output**
```json
{"text": "You are a financial analyst. Summarize the given document as 5-6 crisp bullet points...\n\n..."}
```

## How it's used

This particular skill's content is plain prose, not the reserved
`call_next` JSON shape (see README.md's "Chain calls" section — a text
skill *can* hand off, but only if its content is exactly that shape), so it
always returns itself verbatim rather than handing off. It isn't a hop in
the `pdf-fetcher` → `pdf-summarizer` chain, then; it's fetched separately,
by the orchestrator, before the chain starts:

```python
prompt = orchestrator.invoke("financial-summary-prompt", "1.0.0", {})["text"]
result = orchestrator.invoke("pdf-fetcher", "1.0.0", {"url": pdf_url, "system_prompt": prompt})
```

`pdf-fetcher` threads `system_prompt` through unchanged into the
`call_next` input it hands to `pdf-summarizer`, which uses it (falling back
to a generic instruction if none was supplied) as the system message for
its OpenRouter call. See `examples/chain_fetch_and_summarize_pdf.py`.

Swapping which document gets which prompt is then just a matter of
publishing a different `text` skill and pointing at it — a prompt library
gets the same catalog, versioning, and access-control story as any other
skill, rather than being a string hardcoded into whichever script happens
to call the LLM.
