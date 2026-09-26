# Example skills

Real skill payloads, as standalone files — not embedded strings — so you can
read, copy, or publish them directly. Each subfolder is one skill: its
`run.py` (or `skill.md`/`prompt.md`, for the `text` and combo skills) is the
exact payload published by the corresponding example script, and its
`README.md` explains what it does, its manifest (capabilities, schemas), and
how it fits into the larger example. Between them, these skills demonstrate
every combination this protocol supports: pure code (Python), pure text, and
a combo bundling both together.

| Skill | Used by | What it does |
|---|---|---|
| [`webpage-pdf-finder/`](webpage-pdf-finder/) | `examples/find_and_summarize_pdfs.py` | Fetches a page, returns every PDF link on it |
| [`financial-summary-prompt/`](financial-summary-prompt/) | `examples/chain_fetch_and_summarize_pdf.py` | `text` runtime — not code. Instructions steering a summary toward Revenue/EBITDA/Profit |
| [`pdf-fetcher/`](pdf-fetcher/) | both `chain_fetch_and_summarize_pdf*.py` scripts | Fetches a PDF, extracts its text, hands off via `call_next` to whichever summarizer it's told to |
| [`pdf-summarizer/`](pdf-summarizer/) | `examples/chain_fetch_and_summarize_pdf.py` | Summarizes text via Gemini/OpenRouter, returns the final result |
| [`pdf-summarizer-combo/`](pdf-summarizer-combo/) | `examples/chain_fetch_and_summarize_pdf_combo.py` | Same job as `pdf-summarizer/`, but with its prompt bundled directly in — code + text as one skill |

Three example scripts, each showing a different way to combine `__net_fetch__`
(see README.md's "Network access" section) and a prompt:

- **`find_and_summarize_pdfs.py`** — one skill fetches (`webpage-pdf-finder`);
  the orchestrator's own trusted code does everything after that
  (downloading PDFs, extracting text, calling an LLM). Keeps API keys and
  raw binary data out of sandboxed skill code. The default pattern to reach
  for.
- **`chain_fetch_and_summarize_pdf.py`** — the whole pipeline is expressed as
  chained skills (`pdf-fetcher` → `pdf-summarizer`), with the orchestrator
  following the `call_next` hand-off between them itself, plus a `text`
  skill (`financial-summary-prompt`) fetched up front and threaded through
  as the LLM's instructions rather than hardcoded into `pdf-summarizer`'s own
  code.
- **`chain_fetch_and_summarize_pdf_combo.py`** — the same chain, but the
  prompt is bundled directly into `pdf-summarizer-combo` instead of fetched
  as a separate skill (see that skill's README for the trade-off between the
  two approaches).

A `text` skill can also hand off via `call_next` on its own — declaratively,
by having its content be exactly that reserved JSON shape rather than prose
(see README.md's "Chain calls" section) — though none of the three scripts
above happen to use that particular form; `financial-summary-prompt`'s
content is plain instructions, so it always returns itself verbatim.

The skill files here are read directly by their example scripts at publish
time (`Path(__file__).parent / "skills" / "<id>" / "run.py"`) — they're the
actual source, not documentation of it.
