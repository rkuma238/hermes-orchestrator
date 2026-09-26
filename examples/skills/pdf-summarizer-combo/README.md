# pdf-summarizer-combo

The same job as [`pdf-summarizer/`](../pdf-summarizer/) — summarize text via
Gemini through OpenRouter — but demonstrating a **different way to pair a
prompt with code**: instead of fetching a separate `text` skill
(`financial-summary-prompt`) and threading its content through the chain's
input, this skill **bundles its own prompt file directly**, published and
versioned as one unit with the code that uses it.

## Manifest

| Field | Value |
|---|---|
| `runtime` | `python3.13` |
| `entrypoint` | `run.py:run` |
| `bundle_files` | `["run.py", "prompt.md"]` — published via `files`, not `code` |
| `capabilities` | `net:https://openrouter.ai/*`, `env:OPENROUTER_API_KEY` |

**Input**
```json
{"text": "extracted document text...", "source_url": "https://example.com/report.pdf"}
```

**Output** — identical shape to `pdf-summarizer/`
```json
{"summary": "* bullet one\n* bullet two\n...", "source_url": "https://example.com/report.pdf"}
```

## How it works

Published with `files: {"run.py": ..., "prompt.md": ...}` instead of a
single `code` string (see README.md's "Multi-language, plain-text, and
chained skills" section and spec/SPEC.md's "Combo skills"). The registry
hashes and stores both files as one bundle — tampering with `prompt.md`
would be caught by the same checksum verification as tampering with
`run.py`. At execution time, both files are exposed to the running code as
`__bundle__` (an in-memory mapping, not a real file the code opens by path);
this skill reads its own prompt via `__bundle__["prompt.md"]`.

## Two valid patterns, one choice to make

| | Separate `text` skill (`pdf-summarizer` + `financial-summary-prompt`) | Bundled combo (`pdf-summarizer-combo`) |
|---|---|---|
| Prompt versioned | Independently of the code that uses it | Together with the code, as one unit |
| Reused across skills | Yes — any skill can fetch the same prompt | No — baked into this one skill |
| Swap the prompt without touching code | Yes — publish a new prompt version, repoint | No — requires republishing this skill |
| Extra step at call time | Orchestrator fetches the prompt separately, threads it through | None — it travels with the code automatically |

Neither is "more correct" — it's a genuine trade-off between reuse
(separate skill) and self-containment (bundle). See
`examples/chain_fetch_and_summarize_pdf_combo.py` for this skill wired into
the same `pdf-fetcher` → `pdf-summarizer-combo` chain.
