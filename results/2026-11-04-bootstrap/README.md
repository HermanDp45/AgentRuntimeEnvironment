# Проверка основы · 2026-11-04

Проверял агент Codex. Это проверка подготовки проекта, не БАФ-бенчмарк.
Код: [`87e97dd`](https://github.com/HermanDp45/AgentRuntimeEnvironment/commit/87e97ddd7b2395406d6519aecfd81b71db41e996).
Требования курса: `b42540f5215b4ad30820bdd0330c466ba19e19e1`.

## Среда

Mac, arm64; контейнеры Linux/arm64 в Docker Desktop.
Python 3.13.15, uv 0.12.12, Docker Engine 27.3.1, Compose 2.30.3.
Точные зависимости — в `uv.lock`, базовые образы и digest — в Dockerfile.
Модель, API-ключ и пользовательские файлы не использовались.

## Воспроизведение

Из корня проекта:

```bash
make setup
make test-ci
make run
# В другом терминале:
curl --fail http://127.0.0.1:8080/health
# Остановить make run через Ctrl+C, затем:
docker compose up --build --wait --wait-timeout 60
docker compose ps --format '{{.Service}} {{.State}} {{.Health}}'
curl --fail http://127.0.0.1:8080/health
docker compose down -v --remove-orphans
docker compose ps --all --quiet
```

## Результат

Lock-файл согласован, Ruff и HTTP-тест прошли без внешней сети после установки.
Оба запуска вернули `{"status":"ok"}`. Compose получил healthy,
после очистки список контейнеров проекта пуст. Существующий `.env`
с контрольным комментарием пережил повторный `make setup` без изменений.

Дополнительно проверены UID 10001, `NoNewPrivs: 1`, нулевой `CapEff`
и отказ записи в `/app` с `EROFS`. Это свойства контейнера gateway;
тесты безопасности сандбокса будут реализованы отдельно.

Исходный вывод команд: [test-ci.txt](test-ci.txt), [launch.txt](launch.txt).
Время pytest в выводе — информация о конкретном запуске, не замер производительности.
После проверки контейнеры и локальный сервер остановлены; образы и кеш сборки сохранены.
