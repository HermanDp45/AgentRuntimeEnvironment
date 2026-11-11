# AgentRuntimeEnvironment: среда исполнения агента

## Что это

Проект по [заданию A курса ИТМО × DeepPavlov](https://github.com/deeppavlov/agentic-course-itmo/blob/main/projects/A-agent-runtime.md):
десктоп-клиент подключается к серверу, агент исполняет команды в сандбоксе
и работает с локальной папкой клиента через сетевое монтирование.

**Работает первый агентный сценарий:** gateway → собственный харнес → LLM-прокси →
Docker-сандбокс. Есть чат на PySide6 и консольный клиент, чтение, diff и подтверждение записи/команд,
SSE, отмена и очистка. Реальная Qwen проверена на синтетическом файле.
В чате можно выбрать папку на Mac и подключить её к локальному Docker-серверу.
Файлы видны в Finder и во вкладке «Файлы», правки сохраняются после подтверждения.
Стек сервера — Python/FastAPI и HTTP/JSON + SSE; клиент — PySide6.
Локальное подключение использует WebDAV и rclone/NFS. Удалённый режим проверен
отдельно в [эксперименте с SSH](docs/workdir-demo.md).

Репозиторий следует [официальному шаблону](https://github.com/deeppavlov/agentic-course-itmo/tree/main/projects/templates/track-a-agent-runtime).
Прогресс — [TASKS.md](TASKS.md), причины изменений — [журнал](docs/action-log.md),
простое объяснение задачи — [docs/project.md](docs/project.md).

## Архитектура

```mermaid
flowchart LR
    subgraph Mac[Компьютер пользователя]
        Client[Десктоп-клиент]
        Folder[Локальная папка]
    end
    subgraph Linux[Linux-сервер · Docker Compose]
        Gateway[Gateway: подключение и события]
        Harness[Харнес: агентный цикл]
        Proxy[LLM-прокси]
        Manager[Менеджер сандбоксов]
        Sandbox[Сандбокс · /work]
        Gateway --> Harness
        Harness --> Proxy
        Harness --> Manager --> Sandbox
    end
    Client <--> Gateway
    Folder <-->|Сетевое монтирование| Sandbox
    Proxy --> Model[API модели]
```

Это целевая схема. В Compose работают все четыре серверных компонента,
одноразовый sandbox-image проверяет образ агента. Контейнеры сессий создаёт менеджер.
На macOS выбранная папка подключается к локальному Docker; удалённая машина ещё не проверена.
[ADR-0003](docs/adr/0003-course-template.md) описывает структуру,
[ADR-0002](docs/adr/0002-workdir-mount.md) — предложение по монтированию.
Статусы остальных решений — в [docs/adr/](docs/adr/README.md).

## Запуск сервера

Нужны Git, [uv](https://docs.astral.sh/uv/getting-started/installation/), Make
и запущенный Docker с Compose v2 (на Mac — Docker Desktop).
Python 3.13.15 и зависимости закреплены; используем uv 0.12.12.

```bash
git clone https://github.com/HermanDp45/AgentRuntimeEnvironment.git
cd AgentRuntimeEnvironment
make setup
docker compose up --build
```

`make setup` выполняет `uv sync --frozen` и создаёт `.env` из `.env.example`,
не перезаписывая существующий файл. Compose запускается и без `.env`.
Используются `GATEWAY_PORT` (8080) и `SANDBOX_MANAGER_PORT` (8081), оба на loopback.
Лимиты — в config.toml харнеса и сандбокс-менеджера. Для реальных задач настройте
LLM_PROVIDER_URL, LLM_MODEL и LLM_API_KEY в локальном .env: ключ получает только llm-proxy.
Без ключа сервисы запускаются, но запрос к модели завершается понятной ошибкой.
CLIENT_TOKENS задаёт клиентские токены через запятую; локальное значение по умолчанию — local-dev-token.

В другом терминале:

```bash
curl --fail http://127.0.0.1:8080/health
curl --fail http://127.0.0.1:8081/health
make test-infra
docker compose down -v --remove-orphans
```

Ожидаемый ответ: `{"status":"ok"}`. `make up` ждёт healthy, `make down` выполняет очистку.
Локально без контейнера: `make run`, адрес `127.0.0.1:8080`, остановка — `Ctrl+C`.
Dockerfile каждого компонента находится в его каталоге; build context — корень с общим lock-файлом.

## Клиент

Базовый чат: сообщения, журнал действий и вывод команд, diff и кнопки разрешения/отказа,
отмена и просмотр файлов после задачи. После запуска сервера:

```bash
make setup-gui
make gui
```

Нажмите «Выбрать папку…», затем «Подключить чат». Во вкладке «Файлы» будет видна
выбранная папка. «Обновить файлы» показывает правки, сделанные с компьютера.
Для проверки можно создать note.txt с текстом original и попросить заменить его на changed.
Следующие сообщения используют ту же сессию и историю. Ответы модели появляются целиком;
вывод команд приходит по мере выполнения. Запись и команды ждут отдельного разрешения.
Закрытие окна освобождает сандбокс и сохраняет выбранную папку.
Если папка не выбрана, подключается учебная фикстура.

Консольный одноразовый сценарий также сохранён:

```bash
uv run --frozen --offline python -m client.cli
```

Он создаёт учебный note.txt с текстом original и просит модель заменить его на changed.
После разрешения показывается результат и файлы; закрытие удаляет временную фикстуру.
Папка Mac не затрагивается. Для собственного токена задайте CLIENT_TOKEN, адрес — через --url.
[Команды и ограничения клиента](client/README.md), [как работает цикл](docs/harness.md).

Для LLM-прокси включён DNS через TCP (dns_opt: use-vc): в текущей VPN-сети
публичные DNS не отвечают по UDP. Обычный make up уже содержит исправление;
ручной IP, DNS-overlay и изменение настроек VPN не нужны.
[Проверка без ручного IP](results/2026-11-06-dns-fix/README.md),
[диагностика причины](results/2026-11-06-dns-investigation/README.md).

Сборка для локальной демонстрации: `make build-client`, результат —
`dist/AgentRuntime.app` на macOS. Проверен запуск на текущей macOS arm64;
распространение, notarization и другие платформы ещё не проверены.

Для выбранной папки shell-команды имеют доступ на чтение. Правки проходят через
write_file с подтверждением и проверкой версии. Проверено на macOS
с Docker Desktop. Linux, Windows и удалённое подключение в GUI требуют отдельной проверки.
[Границы и устройство](docs/adr/0010-local-workdir.md).

## Тесты

| Команда | Что запускает |
|---|---|
| `make check` | Согласованность lock-файла, Ruff и проверку форматирования |
| `make test-unit` | Lock/Ruff и unit-тесты без Docker |
| `make test-ci` | Unit, отдельный Compose, инфраструктура, mock UI, smoke-БАФ и очистка |
| `make test-ui` | QtTest: чат против mock HTTP/SSE, после `make setup-gui` |
| `make test-infra` | Только прямые HTTP-тесты сандбокса на обычном Compose, без LLM |
| `make up-test` / `make test-agent` / `make down-test` | Отдельный offline-стенд: вся серверная цепочка с mock LLM, сандбокс и CLI |
| `make baf-early` | Текущий набор через настроенную модель, JSONL с результатами |
| `make baf-smoke` | Простые Б1–Б8 и три Б9 на offline-стенде, сценарный replay |
| `make baf` | 39 задач × 3 повтора через локальный mount на настроенной модели |
| `make build-client` | Собрать macOS-приложение существующего Qt-клиента |
| `make bench-fs` | Пока недоступно: замеры монтирования A-3.6 |
| `make bench-sandbox` | Пока недоступно: замеры сандбокса A-5.6 |

Недоступные цели завершаются ошибкой, а не изображают успешную проверку.
Перед полным CI нужны `make setup-gui`, запущенный Docker и подготовленные образы.
`make test-ci` поднимает отдельный Compose с mock LLM и удаляет его при выходе.
Тесты не требуют внешнего API; первая сборка может скачивать образы и зависимости.
GitHub Actions использует ту же команду. UI проверяется с mock-сервером; локальный
mount с Docker Desktop отдельно проверяется через `HARNESS_TEST_URL` в `make test-ui`.

В наборе есть проверки реального срабатывания CPU/RAM/PID-лимитов, заполнения диска,
таймаутов, canary-секретов, отмены и перезапуска менеджера. Нагрузочные проверки
требуют cgroup v2 и хотя бы два доступных CPU. Полнота A-8 ещё не достигнута:
падение харнеса во время команды проверено: таймаут менеджера завершает процесс,
состояние запуска после рестарта теряется. Восстановление удалённого mount остаётся открытым.
Сценарный replay не измеряет качество LLM. [Устройство БАФ](baf/README.md).

## Структура

```text
services/          gateway, harness, sandbox-manager, llm-proxy
client/            PySide6-чат, CLI и общий HTTP/SSE-транспорт
contracts/         схемы обоих протоколов с версиями
baf/               курсовой каталог, первые Б1–Б5 и раннер
tests/unit/        контракты, харнес, транспорт, gateway, proxy и файлы
tests/ui/          QtTest против mock HTTP/SSE и опционально Compose
tests/infra/       весь сервер с mock LLM, настоящий Docker, CLI и перезапуск
docs/adr/          решения и шаблон ADR
docs/harness.md    карта реализации харнеса
docs/baf.md        свои БАФ и обоснование
docs/report.md     замеры, матрица БАФ × пара, выводы
results/           сырые результаты и команды воспроизведения
```

Исходники и Dockerfile каждого реализованного сервиса находятся в его каталоге.
Папки будущих компонентов содержат описание статуса, без фиктивных сервисов.
Дополнительно: [AGENTS.md](AGENTS.md), [TASKS.md](TASKS.md), [журнал действий](docs/action-log.md).

## Модели

Временный доступ к Qwen/Qwen3-235B-A22B-Instruct-2507 через inference.airi.net
проверен сначала отдельно, затем через gateway, харнес, proxy и настоящий сандбокс.
[Первый агентный прогон](results/2026-11-06-harness-v0/README.md) — чтение фикстуры,
не оценка качества и не официальный БАФ. В offline CI используется сценарная модель.
Для официальных БАФ пары ещё не выбраны. При выборе проверяем разрешённость
по [общим правилам курса](https://github.com/deeppavlov/agentic-course-itmo/blob/main/projects/README.md#0-1-модели),
записываем модель, версию, AA Intelligence Index и дату проверки в [отчёт](docs/report.md).
Набор расширен до 39 задач; offline replay и реальные модельные результаты
учитываются отдельно. Первые Б1–Б5 ранее прошли на техническом прогоне с Qwen. Для курсового замера остаётся
повторить на разрешённой модели. [Результаты и команда](results/2026-11-10-baf-early/README.md).
Секреты хранятся только локально; исходные результаты — в Git после удаления секретов.

К ЧП1: [официальные вопросы](https://github.com/deeppavlov/agentic-course-itmo/blob/main/projects/checkpoint-1-questions.md)
— общий блок и направление «Без арены».
[Ответы по нашему решению и демонстрация](docs/checkpoint-1-answers.md);
общее объяснение — в [разборе проекта](docs/project.md).
