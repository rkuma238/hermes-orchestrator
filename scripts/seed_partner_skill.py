"""Seeds 'partner-currency-convert', a skill whose manifest+discovery live in
the main registry but whose payload is served by partner_service — proving
Envoy can route a single skill's payload fetch to a distinct backend while
discovery/manifest stay centralized. Run once:

    python -m scripts.seed_partner_skill
"""

import hashlib
import json
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

PAYLOAD_SRC = """def run(input_data):
    amount = input_data["amount"]
    rate = {"USD_EUR": 0.92, "EUR_USD": 1.09}.get(input_data["pair"])
    if rate is None:
        raise ValueError(f"unsupported pair: {input_data['pair']}")
    return {"converted": round(amount * rate, 2), "rate": rate}
"""

SKILL_ID = "partner-currency-convert"
VERSION = "1.0.0"


def main():
    digest = hashlib.sha256(PAYLOAD_SRC.encode("utf-8")).hexdigest()

    manifest = {
        "osp_version": "0.1",
        "id": SKILL_ID,
        "version": VERSION,
        "name": "Currency Convert (partner)",
        "description": "Converts an amount between USD and EUR. Served by an independent partner backend, routed to by Envoy on path alone.",
        "runtime": "python3.13",
        "entrypoint": "payload.py:run",
        "input_schema": {
            "type": "object",
            "properties": {
                "amount": {"type": "number"},
                "pair": {"type": "string", "enum": ["USD_EUR", "EUR_USD"]},
            },
            "required": ["amount", "pair"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"converted": {"type": "number"}, "rate": {"type": "number"}},
            "required": ["converted", "rate"],
        },
        "capabilities": [],
        "resource_limits": {"timeout_seconds": 10, "max_memory_mb": 256},
        "payload": {
            # Routed by Envoy's /partner/ prefix rule to partner_service,
            # not to the main registry that's serving this very manifest.
            "url": f"/partner/{SKILL_ID}/{VERSION}/payload",
            "sha256": digest,
        },
        "publisher": {"name": "example-partner-co"},
        "visibility": "public",
        "allowed_accounts": [],
    }

    # 1. Main registry: owns discovery + manifest for this skill id.
    registry_dir = REPO_ROOT / "registry_server" / "skills_store" / SKILL_ID / VERSION
    registry_dir.mkdir(parents=True, exist_ok=True)
    (registry_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {registry_dir / 'manifest.json'} (registry copy, no payload.py needed here)")

    # 2. Partner backend: owns the actual payload bytes + its own copy of the
    #    manifest, used only to make its own visibility/ACL decision locally.
    partner_dir = REPO_ROOT / "partner_service" / "skills_store" / SKILL_ID / VERSION
    partner_dir.mkdir(parents=True, exist_ok=True)
    (partner_dir / "payload.py").write_text(PAYLOAD_SRC)
    (partner_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {partner_dir / 'manifest.json'} and payload.py (partner copy)")


if __name__ == "__main__":
    main()
