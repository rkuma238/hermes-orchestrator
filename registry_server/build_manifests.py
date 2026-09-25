"""Generates manifest.json for each example skill from its payload.py, so the
checksum in the manifest always matches the bytes actually being served.

Run this whenever an example payload.py changes:
    python registry_server/build_manifests.py
"""

import hashlib
import json
from pathlib import Path

STORE = Path(__file__).parent / "skills_store"

SKILLS = [
    {
        "id": "example-echo",
        "version": "1.0.0",
        "name": "Echo",
        "description": "Echoes back a message and reports its length. Trivial reference skill for testing discovery/fetch/execute.",
        "runtime": "python3.13",
        "entrypoint": "payload.py:run",
        "input_schema": {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
        },
        "output_schema": {
            "type": "object",
            "properties": {"echo": {"type": "string"}, "length": {"type": "integer"}},
            "required": ["echo", "length"],
        },
        "capabilities": [],
    },
    {
        "id": "example-word-count",
        "version": "1.0.0",
        "name": "Word Count",
        "description": "Counts words and characters in a block of text.",
        "runtime": "python3.13",
        "entrypoint": "payload.py:run",
        "input_schema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "word_count": {"type": "integer"},
                "char_count": {"type": "integer"},
            },
            "required": ["word_count", "char_count"],
        },
        "capabilities": [],
    },
]


def main():
    for skill in SKILLS:
        skill_dir = STORE / skill["id"] / skill["version"]
        payload_path = skill_dir / "payload.py"
        payload_bytes = payload_path.read_bytes()
        digest = hashlib.sha256(payload_bytes).hexdigest()

        manifest = {
            "protocol_version": "0.1",
            "id": skill["id"],
            "version": skill["version"],
            "name": skill["name"],
            "description": skill["description"],
            "runtime": skill["runtime"],
            "entrypoint": skill["entrypoint"],
            "input_schema": skill["input_schema"],
            "output_schema": skill["output_schema"],
            "capabilities": skill["capabilities"],
            "resource_limits": {"timeout_seconds": 10, "max_memory_mb": 256},
            "payload": {
                "url": f"/skills/{skill['id']}/{skill['version']}/payload",
                "sha256": digest,
            },
            "publisher": {"name": "skillward-reference-examples"},
            "visibility": "public",
            "allowed_accounts": [],
        }

        manifest_path = skill_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        print(f"wrote {manifest_path} (sha256={digest[:12]}...)")


if __name__ == "__main__":
    main()
