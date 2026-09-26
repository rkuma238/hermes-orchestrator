# Example skills

Real skill payloads, as standalone files — not embedded strings — so you can
read, copy, or publish them directly. Each subfolder is one skill: its
`run.py` is the exact code published by the corresponding example script,
and its `README.md` explains what it does, its manifest (capabilities,
schemas), and how it fits into the larger example.

| Skill | Used by | What it does |
|---|---|---|
| [`webpage-pdf-finder/`](webpage-pdf-finder/) | `examples/find_and_summarize_pdfs.py` | Fetches a page, returns every PDF link on it |
| [`pdf-fetcher/`](pdf-fetcher/) | `examples/chain_fetch_and_summarize_pdf.py` | Fetches a PDF, extracts its text, hands off via `call_next` |
| [`pdf-summarizer/`](pdf-summarizer/) | `examples/chain_fetch_and_summarize_pdf.py` | Summarizes text via Gemini/OpenRouter, returns the final result |

These two example scripts show the same underlying capability
(`__net_fetch__`, see README.md's "Network access" section) used two
different ways:

- **`find_and_summarize_pdfs.py`** — one skill fetches (`webpage-pdf-finder`);
  the orchestrator's own trusted code does everything after that
  (downloading PDFs, extracting text, calling an LLM). Keeps API keys and
  raw binary data out of sandboxed skill code. The default pattern to reach
  for.
- **`chain_fetch_and_summarize_pdf.py`** — the whole pipeline is expressed as
  two chained skills (`pdf-fetcher` → `pdf-summarizer`), with the
  orchestrator following the `call_next` hand-off between them itself. Shows
  a real two-hop chain doing substantive work at each step, at the cost of
  putting an API key and binary PDF bytes inside the sandbox.

The skill files here are read directly by their example scripts at publish
time (`Path(__file__).parent / "skills" / "<id>" / "run.py"`) — they're the
actual source, not documentation of it.
