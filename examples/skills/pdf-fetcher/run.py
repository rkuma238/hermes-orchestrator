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

    return {
        "call_next": {
            "id": "pdf-summarizer",
            "version": "1.0.0",
            "input": {"text": text[:20000], "source_url": url},
        }
    }
