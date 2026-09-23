"""Offline stdio fixture; refuses to operate outside the E2E sandbox."""

import json
import os
import sys
from pathlib import Path


def main() -> int:
    root = Path(os.environ["E2E_ROOT"]).resolve()
    home = Path(os.environ["CODEX_HOME"]).resolve()
    if not home.is_relative_to(root):
        raise RuntimeError("Refusing a home outside the E2E sandbox")
    arguments = sys.argv[1:]
    event = {
        "args": arguments,
        "home": str(home),
        "sqlite_home": os.environ.get("CODEX_SQLITE_HOME"),
        "auth_overrides": [
            name
            for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN")
            if name in os.environ
        ],
    }

    def log(value: dict) -> None:
        with (root / "events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(value) + "\n")

    log(event)
    if arguments == ["app-server"]:
        initialized = False
        for line in sys.stdin:
            request = json.loads(line)
            log({"rpc": request})
            method = request["method"]
            if method == "initialize":
                result = {"userAgent": "codex-accounts-e2e"}
            elif method == "initialized":
                initialized = True
                continue
            elif method == "account/read":
                if not initialized or request.get("params") != {"refreshToken": False}:
                    return 91
                mode = os.environ.get("E2E_METADATA", "normal")
                if mode == "exit":
                    return 92
                if mode == "malformed":
                    print("[]", flush=True)
                    continue
                if mode == "error":
                    print(
                        json.dumps(
                            {"id": request["id"], "error": {"message": "FAKE-SECRET"}}
                        ),
                        flush=True,
                    )
                    continue
                credential = home / "auth.json"
                account = (
                    json.loads(credential.read_text(encoding="utf-8"))
                    if credential.exists()
                    else None
                )
                result = {"account": account}
            elif method == "account/rateLimits/read":
                result = {
                    "rateLimits": {
                        "primary": {"windowDurationMins": 300, "usedPercent": 25},
                        "secondary": {"windowDurationMins": 10080, "usedPercent": 50},
                    }
                }
            else:
                return 93
            print(json.dumps({"id": request["id"], "result": result}), flush=True)
        return 0
    if arguments and arguments[0] in ("login", "logout"):
        return 94
    if arguments == ["--e2e-exit"]:
        return 37
    if arguments == ["--e2e-stdin"]:
        print(sys.stdin.read(), end="")
        return 0
    print(json.dumps(event), flush=True)
    return 0
