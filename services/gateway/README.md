# gateway

Статус: реализован только `GET /health` → `200 {"status":"ok"}`.
Аутентификация, сессии, сообщения и поток событий пока не реализованы.

Исходники — в `src/agent_runtime/`, Dockerfile — в этом каталоге.
Сборка использует корень репозитория как context: здесь общий `pyproject.toml` и `uv.lock`.
Запуск из корня: `make run` или `docker compose up --build`.
API-ключ модели и остальные секреты gateway сейчас не получает.

Основание структуры — [ADR-0003](../../docs/adr/0003-course-template.md).
