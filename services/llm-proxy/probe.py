"""Два синтетических запроса к API; инструменты и файлы пользователя не исполняются."""

import json
import os
import sys
import time
from pathlib import Path

import httpx2

MODEL = "Qwen/Qwen3-235B-A22B-Instruct-2507"


def main():
    if len(sys.argv) != 2:
        raise SystemExit("Usage: probe.py <output.json>")
    destination = Path(sys.argv[1])
    key = os.environ["LLM_API_KEY"]
    endpoint = os.environ["LLM_PROVIDER_URL"].rstrip("/") + "/v1/chat/completions"
    if not endpoint.startswith("https://"):
        raise SystemExit("HTTPS is required")
    tool = {
        "type": "function",
        "function": {
            "name": "get_service_port",
            "description": "Get the configured port of a service.",
            "parameters": {
                "type": "object",
                "properties": {"service": {"type": "string"}},
                "required": ["service"],
                "additionalProperties": False,
            },
        },
    }
    cases = [
        ("text", "Reply with exactly OK and nothing else.", {}),
        (
            "tool_call",
            "Call get_service_port with service equal to orders. Do not answer directly.",
            {"tools": [tool], "tool_choice": "auto"},
        ),
    ]
    report = {"endpoint": endpoint, "model": MODEL, "checks": []}
    for name, prompt, extra in cases:
        payload = {
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 8 if name == "text" else 128,
            "temperature": 0,
            **extra,
        }
        check = {"name": name, "request": payload, "pass": False}
        started = time.monotonic()
        try:
            response = httpx2.post(
                endpoint,
                json=payload,
                headers={"Authorization": f"Bearer {key}"},
                timeout=60,
                follow_redirects=False,
            )
            check["http_status"] = response.status_code
            response.raise_for_status()
            body = response.json()
            check["response"] = body
            message = body["choices"][0]["message"]
            if name == "text":
                check["pass"] = (message.get("content") or "").strip() == "OK"
            else:
                calls = message.get("tool_calls") or []
                check["pass"] = any(
                    call["function"]["name"] == "get_service_port"
                    and json.loads(call["function"]["arguments"]) == {"service": "orders"}
                    for call in calls
                )
        except (httpx2.HTTPError, ValueError, KeyError, IndexError) as error:
            check["error"] = type(error).__name__
        check["seconds"] = round(time.monotonic() - started, 3)
        report["checks"].append(check)
        print(json.dumps({k: v for k, v in check.items() if k not in ("request", "response")}))
        if "error" in check:
            break
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(report, ensure_ascii=False, indent=2).replace(key, "REDACTED") + "\n"
    )
    return 0 if len(report["checks"]) == 2 and all(c["pass"] for c in report["checks"]) else 1


if __name__ == "__main__":
    sys.exit(main())
