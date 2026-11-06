"""Сценарные ответы только для offline-инфраструктурных тестов."""

import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

app = FastAPI()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def completion(request: Request):
    if request.headers.get("Authorization") != "Bearer fixture-provider-secret":
        return JSONResponse(status_code=401, content={"error": "Wrong test credential"})
    body = await request.json()
    history = body["messages"]
    index = max(index for index, item in enumerate(history) if item["role"] == "user")
    task = history[index]["content"]
    outputs = [
        json.loads(item["content"]) for item in history[index + 1 :] if item["role"] == "tool"
    ]
    count = len(outputs)
    name, arguments, text = None, None, "Готово"
    if task == "test:edit":
        if count == 0:
            name, arguments = "read_file", {"path": "note.txt"}
        elif count == 1:
            name, arguments = (
                "write_file",
                {
                    "path": "note.txt",
                    "content": "changed",
                    "expected_sha256": outputs[-1]["value"]["sha256"],
                },
            )
        elif count == 2:
            name, arguments = "run_command", {"command": "echo output; echo failure >&2; exit 1"}
        else:
            exit_code = outputs[-1]["value"].get("exit_code")
            text = f"Файл изменён; команда завершилась с кодом {exit_code}."
    elif task == "test:deny" and not outputs:
        name, arguments = "run_command", {"command": "touch forbidden.txt"}
    elif task == "test:cancel" and not outputs:
        name, arguments = (
            "run_command",
            {
                "command": (
                    'python -c \'import time; open("saved.txt","w").write("saved"); '
                    'print("ready",flush=True); time.sleep(300)\' '
                )
            },
        )
    elif task == "test:invalid" and not outputs:
        name, arguments = "read_file", {"path": "../outside"}
    elif task == "test:history":
        text = " | ".join(item["content"] for item in history if item["role"] == "user")
    elif not task.startswith("test:"):
        return JSONResponse(status_code=400, content={"error": "Unknown fixture scenario"})
    message = {"role": "assistant", "content": None if name else text}
    if name:
        message["tool_calls"] = [
            {
                "id": f"tool-{len(history)}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ]
    return {
        "model": "fixture-model",
        "choices": [{"message": message, "finish_reason": "tool_calls" if name else "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
