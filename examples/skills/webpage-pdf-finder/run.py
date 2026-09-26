def run(input_data):
    import re
    from urllib.parse import urljoin

    url = input_data["url"]
    resp = __net_fetch__(url, headers={"User-Agent": "Mozilla/5.0 (compatible; SkillwardBot/1.0)"})
    html = resp["body"]
    matches = re.findall(r'href=["\']([^"\'>]+?\.pdf[^"\'>]*)["\']', html, re.IGNORECASE)
    seen = []
    for m in matches:
        full = urljoin(url, m)
        if full not in seen:
            seen.append(full)
    return {"pdf_links": seen, "count": len(seen)}
