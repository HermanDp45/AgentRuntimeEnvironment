# Сандбокс и контракты v0 — 2026-11-06

Проверял агент на macOS arm64, Docker Desktop: Engine 27.3.1, API 1.47,
Compose 2.30.3, Python 3.13.15, uv 0.12.12. Тесты содержат только синтетические
файлы; API модели не вызывался. Все зависимости и базовые образы закреплены в репозитории.

## Воспроизведение

Из корня проекта, с запущенным Docker Desktop:

```bash
make setup
make test-ci
make up
make test-infra
uv run --frozen --offline python tests/infra/check_lifecycle.py
make down
```

`make test-ci`: 28 passed, 4 skipped. Четыре HTTP-сценария требуют Compose,
запускаются через `make test-infra`: 4 passed. Unit-тесты выполняются offline.
`check_lifecycle.py` предназначен для локального Compose без других активных сессий
и оставляет менеджер остановленным; удаляет только контейнеры своего namespace.

## Исходные результаты

- `setup.txt` — установка по lock-файлу с сохранением существующего .env.
- `gateway-health.json`, `manager-health.json` — ответы команд README.
- `unit.txt` — lock, Ruff, форматирование, pytest.
- `build.txt` — сборка всех образов и переход gateway/менеджера в healthy.
- `infra.txt` — настоящие HTTP-сценарии, не mock Docker.
- `lifecycle.txt` — настройки контейнера, startup_orphan_cleanup и graceful_cleanup.
- `empty-cancel-diagnostic.txt` — причина исправленного пограничного случая: ESRCH при отсутствии потомков.
- `hygiene.txt` — исключения окружения и проверка отсутствия локального ключа в кандидатах Git.
- `cleanup.txt` — остановка Compose, отсутствие контейнеров проекта и сандбоксов.

HTTP-сценарии проверяют файлы и конфликт SHA, повтор без исполнения, command error,
stdout/stderr и Unicode в SSE, один result, resume, cancel setsid-потомка,
сохранение уже записанного файла, timeout, повторную работу сессии, read-only,
UID/capabilities/seccomp, отсутствие сокета/ключа в сандбоксе, закрытую сеть,
ENOSPC и ограничение вывода, независимые сессии и release во время команды.
Скрипт жизненного цикла сверяет CPU/RAM/PID-настройки, убивает менеджер SIGKILL,
проверяет очистку сироты при старте и очистку новой сессии при штатном shutdown.
Прерывание атомарной публикации файла, симлинки/FIFO, классы ошибок и ускоренный idle
проверяются unit-тестами. Временные резервные копии находятся вне сандбокса и удаляются
в finally; при потере менеджера его приватная tmpfs исчезает с контейнером.

Это проверка локального прототипа. Полного агентного сценария, сетевого mount,
LLM-прокси, клиента, всех проверок A-5 и результатов БАФ здесь ещё нет.
Удалённый Linux CI на a8ded7c также прошёл: python и compose,
включая HTTP-сценарии, перезапуск и очистку.
[Ручной запуск 42224420356](https://github.com/HermanDp45/AgentRuntimeEnvironment/actions/runs/42224420356),
исходный ответ GitHub — ci.json. Автоматический push-запуск не наблюдался.
Публикация этих свидетельств не меняет проверенную реализацию.
