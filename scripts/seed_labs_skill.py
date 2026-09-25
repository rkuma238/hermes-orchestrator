"""Seeds 'labs-reverse-text' on the third backend (labs_service), proving a
new registry was onboarded via config (backends.yaml) + this seed, without
touching registry_server, partner_service, or hand-editing Envoy routes.
Run once:

    python -m scripts.seed_labs_skill
"""

import hashlib
import json
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

PAYLOAD_SRC = """def run(input_data):
    text = input_data["text"]
    return {"reversed": text[::-1]}
"""

SKILL_ID = "labs-reverse-text"
VERSION = "1.0.0"


def main():
    digest = hashlib.sha256(PAYLOAD_SRC.encode("utf-8")).hexdigest()

    manifest = {
        "protocol_version": "0.1",
        "id": SKILL_ID,
        "version": VERSION,
        "name": "Reverse Text (labs)",
        "description": "Reverses a string. Served by a third, independently added backend to prove multi-registry onboarding via config.",
        "runtime": "python3.13",
        "entrypoint": "payload.py:run",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"reversed": {"type": "string"}},
            "required": ["reversed"],
        },
        "capabilities": [],
        "resource_limits": {"timeout_seconds": 10, "max_memory_mb": 256},
        "payload": {
            "url": f"/labs/{SKILL_ID}/{VERSION}/payload",
            "sha256": digest,
        },
        "publisher": {"name": "example-labs-team"},
        "visibility": "public",
        "allowed_accounts": [],
    }

    registry_dir = REPO_ROOT / "registry_server" / "skills_store" / SKILL_ID / VERSION
    registry_dir.mkdir(parents=True, exist_ok=True)
    (registry_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {registry_dir / 'manifest.json'} (registry copy, discovery only)")

    labs_dir = REPO_ROOT / "labs_service" / "skills_store" / SKILL_ID / VERSION
    labs_dir.mkdir(parents=True, exist_ok=True)
    (labs_dir / "payload.py").write_text(PAYLOAD_SRC)
    (labs_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {labs_dir / 'manifest.json'} and payload.py (labs copy)")


if __name__ == "__main__":
    main()
