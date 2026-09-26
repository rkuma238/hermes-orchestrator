# pdf-summarizer

Takes text (typically handed off from `pdf-fetcher` via `call_next`) and
summarizes it using Gemini through OpenRouter. This is hop 2 — the final
result of the chain in `examples/chain_fetch_and_summarize_pdf.py`.

## Manifest

| Field | Value |
|---|---|
| `runtime` | `python3.13` |
| `entrypoint` | `run.py:run` |
| `capabilities` | `net:https://openrouter.ai/*`, `env:OPENROUTER_API_KEY` |

**Input**
```json
{"text": "extracted document text...", "source_url": "https://example.com/report.pdf"}
```

**Output**
```json
{"summary": "* bullet one\n* bullet two\n...", "source_url": "https://example.com/report.pdf"}
```

## How it works

Reads the OpenRouter API key from `os.environ["OPENROUTER_API_KEY"]` —
available only because `env:OPENROUTER_API_KEY` is declared *and* granted by
the deployment (see README.md's capability model: `env:` only ever exposes
the specific named variable it declares, nothing else). Calls OpenRouter's
chat completions endpoint via `__net_fetch__` with a fixed model
(`google/gemini-2.5-flash`) and a short system prompt asking for a 5-6
bullet summary, then returns the model's response text.

**Trade-off worth being explicit about:** this design puts an LLM API key
inside sandboxed skill code. `examples/find_and_summarize_pdfs.py` avoids
that entirely by doing summarization in the orchestrator's own trusted code
instead — reach for that pattern by default. This skill exists to
demonstrate that a chained hop can do real, substantive work (not just pass
a value through), including calling out to a third-party API of its own.

See `examples/chain_fetch_and_summarize_pdf.py` for the full runnable chain.
