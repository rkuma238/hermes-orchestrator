"""Register an account -> discover -> fetch -> verify -> execute, through the
real Envoy gateway (not the registry directly — the registry now rejects
anything without a validated x-account-id, which only Envoy supplies).

Start both first:
    uvicorn registry_server.main:app --port 8079
    envoy -c envoy/envoy.yaml

Then:
    python -m examples.run_end_to_end
"""

import httpx

from skillward import SkillwardOrchestrator

GATEWAY_URL = "http://127.0.0.1:10000"


def main():
    print("== sign up (the one unauthenticated route) ==")
    resp = httpx.post(f"{GATEWAY_URL}/accounts", json={"name": "demo-agent"})
    resp.raise_for_status()
    account = resp.json()
    print(f"  account_id={account['account_id']}")

    with SkillwardOrchestrator(GATEWAY_URL, api_key=account["api_key"]) as orch:
        print("\n== discover (requires auth now) ==")
        for skill in orch.discover():
            print(f"  {skill.id}@{skill.version} [{skill.visibility}] — {skill.description}")

        print("\n== invoke example-echo ==")
        result = orch.invoke("example-echo", "1.0.0", {"message": "hello from an LLM agent"})
        print(" ", result)
        assert result == {"echo": "hello from an LLM agent", "length": 23}

        print("\n== invoke example-word-count ==")
        result = orch.invoke("example-word-count", "1.0.0", {"text": "the quick brown fox"})
        print(" ", result)
        assert result == {"word_count": 4, "char_count": 19}

    print("\nAll skills discovered, fetched, verified, and executed through the gateway.")


if __name__ == "__main__":
    main()
