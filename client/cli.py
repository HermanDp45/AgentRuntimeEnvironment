"""Ручной запуск на учебной фикстуре; папка Mac пока не подключается."""

import argparse
import json
import os
from uuid import uuid4

import httpx2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--task", default="Прочитай note.txt и замени original на changed.")
    args = parser.parse_args()
    session, run = str(uuid4()), str(uuid4())
    token = os.getenv("CLIENT_TOKEN", "local-dev-token")
    with httpx2.Client(
        base_url=args.url,
        headers={"Authorization": "Bearer " + token},
        timeout=httpx2.Timeout(20, connect=5),
        follow_redirects=False,
        trust_env=False,
    ) as client:

        def post(path, kind, **data):
            response = client.post(path, json={"version": "0", "type": kind, **data})
            response.raise_for_status()
            return response.json()

        created = False
        try:
            post(
                "/v0/sessions",
                "session.fixture",
                session_id=session,
                files={"note.txt": "original"},
            )
            created = True
            print("Учебная сессия: временный note.txt содержит original.", flush=True)
            post("/v0/tasks", "task", session_id=session, run_id=run, message=args.task)
            with client.stream("GET", f"/v0/runs/{run}/events") as stream:
                stream.raise_for_status()
                for line in stream.iter_lines():
                    if not line.startswith("data: "):
                        continue
                    event = json.loads(line[6:])
                    if event["type"] == "run.result":
                        result = event["result"]
                        print("Итог:", result["status"], flush=True)
                        if result.get("error"):
                            print(result["error"]["code"], flush=True)
                        if result["status"] == "ok":
                            files = client.get(f"/v0/sessions/{session}/files")
                            files.raise_for_status()
                            print(
                                "Файлы:",
                                json.dumps(files.json()["files"], ensure_ascii=False),
                                flush=True,
                            )
                        return 0 if result["status"] == "ok" else 1
                    data = event["data"]
                    if event["event"] == "message":
                        print(data["text"], flush=True)
                    elif event["event"] == "approval_required":
                        operation = data["operation"]
                        print(
                            "Подтверждение:",
                            operation["kind"],
                            operation.get("path", ""),
                            flush=True,
                        )
                        print(data.get("diff", operation.get("command", "")), flush=True)
                        try:
                            approved = input("Разрешить? [y/N] ").strip().lower() in (
                                "y",
                                "yes",
                                "д",
                                "да",
                            )
                        except EOFError:
                            approved = False
                        post(
                            "/v0/approvals",
                            "approval",
                            session_id=session,
                            run_id=run,
                            operation_id=data["operation_id"],
                            approved=approved,
                        )
                    elif data.get("phase") in ("stdout", "stderr"):
                        print(data["text"], end="", flush=True)
        except KeyboardInterrupt:
            if created:
                post("/v0/cancel", "run.cancel", session_id=session, run_id=run)
            return 130
        except httpx2.HTTPError:
            print(
                "HTTP-запрос не завершён. Проверьте адрес, токен и состояние сервера.", flush=True
            )
            return 1
        finally:
            if created:
                try:
                    post("/v0/close", "session.close", session_id=session)
                except httpx2.HTTPError:
                    print("Не удалось подтвердить закрытие сессии.", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
