def run(input_data):
    import json
    import os

    text = input_data["text"]
    source_url = input_data.get("source_url", "")
    api_key = os.environ["OPENROUTER_API_KEY"]

    # Passed through from pdf-fetcher, which got it from whichever `text`
    # skill the caller chose to fetch first (see
    # examples/skills/financial-summary-prompt/README.md) - falls back to a
    # generic instruction if none was supplied.
    system_prompt = input_data.get(
        "system_prompt",
        "You are a concise analyst. Summarize the given document in 5-6 crisp bullet "
        "points, suitable for a busy reader. Only use facts present in the text.",
    )

    payload = json.dumps(
        {
            "model": "google/gemini-2.5-flash",
            "max_tokens": 400,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": "Summarize this document (" + source_url + "):\n\n" + text},
            ],
        }
    )
    resp = __net_fetch__(
        "https://openrouter.ai/api/v1/chat/completions",
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key},
        body=payload,
    )
    body = json.loads(resp["body"])
    if "choices" not in body:
        raise RuntimeError("openrouter error: " + json.dumps(body)[:500])
    return {"summary": body["choices"][0]["message"]["content"], "source_url": source_url}
