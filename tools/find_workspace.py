"""Print the Anthropic workspace IDs your key can see, and test a real call against each.

A user-scoped key (sk-ant-usr-...) is not bound to a workspace, so the Messages API rejects it with
HTTP 400 unless the request names one via the anthropic-workspace-id header. This finds that ID.

    ./venv/bin/python tools/find_workspace.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

from dotenv import load_dotenv

load_dotenv()
KEY = os.getenv("ANTHROPIC_API_KEY")
MODEL = "claude-sonnet-5-5"


def call(url: str, payload: dict | None = None, workspace: str | None = None):
    """-> (status, parsed body). Never raises on an HTTP error, so we can read the error message."""
    headers = {"x-api-key": KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    if workspace:
        headers["anthropic-workspace-id"] = workspace
    data = json.dumps(payload).encode() if payload else None
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if payload else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"raw": body[:300]}
    except Exception as e:                                  # noqa: BLE001
        return 0, {"error": f"{type(e).__name__}: {e}"}


def ping(workspace: str | None):
    return call("https://api.anthropic.com/v1/messages",
                {"model": MODEL, "max_tokens": 8, "messages": [{"role": "user", "content": "ping"}]},
                workspace)


def main() -> int:
    if not KEY:
        print("ANTHROPIC_API_KEY is not set. Put it in .env (gitignored) and re-run.")
        return 1
    print(f"key prefix: {KEY[:11]}...  ({len(KEY)} chars)\n")

    status, body = ping(None)
    if status == 200:
        print("No workspace header needed - this key already works. Nothing to do.")
        return 0
    print(f"without a workspace header: HTTP {status} - {body.get('error', {}).get('message', body)}\n")

    print("asking the Admin API which workspaces this key can see...")
    status, body = call("https://api.anthropic.com/v1/organizations/workspaces")
    if status == 200:
        rows = [w for w in body.get("data", []) if not w.get("archived_at")]
        if not rows:
            print("  the API returned no active workspaces.")
            return 1
        print(f"  found {len(rows)}:")
        for w in rows:
            print(f"    {w.get('id')}   {w.get('name')}")
        print("\ntesting a real message call against each:")
        for w in rows:
            st, bd = ping(w["id"])
            msg = "WORKS" if st == 200 else f"HTTP {st} - {bd.get('error', {}).get('message', bd)}"
            print(f"    {w.get('id')}  {msg}")
            if st == 200:
                print(f"\nUse this:\n  ANTHROPIC_WORKSPACE_ID={w['id']}")
                return 0
        return 1

    print(f"  HTTP {status} - {body.get('error', {}).get('message', body)}")
    print("\nThis key cannot list workspaces (that needs an Admin key, sk-ant-admin...).")
    print("Get the ID from console.anthropic.com -> Settings -> Workspaces -> click the workspace;")
    print("it is the last part of the browser URL. Then re-run this script to confirm it works.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
