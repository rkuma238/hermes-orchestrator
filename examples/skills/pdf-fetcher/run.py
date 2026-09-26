def run(input_data):
    import base64
    import re
    import zlib

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

    next_input = {"text": text[:20000], "source_url": url}
    # Threaded straight through, unchanged: this skill has no opinion on how
    # the summary should be written, it just carries the instruction along
    # to whichever skill actually calls the LLM. See
    # examples/skills/financial-summary-prompt/README.md.
    if "system_prompt" in input_data:
        next_input["system_prompt"] = input_data["system_prompt"]

    # Which summarizer to hand off to is the caller's choice, not baked in —
    # examples/chain_fetch_and_summarize_pdf.py uses the default
    # (pdf-summarizer, paired with a separately-fetched text skill);
    # examples/chain_fetch_and_summarize_pdf_combo.py points at
    # pdf-summarizer-combo instead (its prompt bundled directly into it).
    next_skill_id = input_data.get("next_skill_id", "pdf-summarizer")
    return {"call_next": {"id": next_skill_id, "version": "1.0.0", "input": next_input}}
