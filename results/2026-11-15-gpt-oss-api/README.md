# GPT-OSS через AIRI — 2026-11-15

Пользователь предложил `Openai/Gpt-oss-120b` как вторую модель и предоставил
пример `/tokenize`. Использован уже настроенный ключ из `.env`, без его вывода.
Рабочая модель приложения не переключалась.

## Результат

- GET `/v1/models`: HTTP 200, точный идентификатор есть в списке.
- POST `/tokenize`: HTTP 200 для `Text to tokenize`, `add_special_tokens=true`.
- POST `/v1/chat/completions`: HTTP 200, ответ `OK`.
- Запрос инструмента: HTTP 200, `get_service_port` с `{"service":"orders"}`.
  Инструмент не исполнялся.

`discovery.json` содержит список моделей и ответ токенизации;
`chat-probe.json` — оба запроса, исходные ответы, токены и длительности.
Текстовая проба заняла 0.411 с, tool call — 0.534 с. Это единичные пробы API,
не замер производительности и не полный БАФ. Лимит ответа — 2048 токенов,
temperature=0; reasoning effort не задан. Модель потратила соответственно
49 и 72 выходных токена, включая 38 и 52 reasoning tokens.

После изменения пробника `make test-ci` завершился с кодом 0: 93 unit,
16 инфраструктурных, 7 UI (2 live UI пропущены), 11 smoke и lifecycle.
Лог — `test-ci.txt`, smoke-ответы и манифест —
`../baf/early/20261115T065553051293Z.*`. Это отдельная проверка с mock LLM.

## Воспроизведение

Из корня репозитория с настроенным `.env`:

```bash
UV_CACHE_DIR=.cache/uv \
LLM_PROBE_MODEL=Openai/Gpt-oss-120b LLM_PROBE_MAX_TOKENS=2048 \
uv run --frozen --offline --env-file .env python services/llm-proxy/probe.py \
  results/2026-11-15-gpt-oss-api/chat-probe.json
```

Для discovery выполнить GET `https://inference.airi.net/v1/models` и POST
`https://inference.airi.net/tokenize` с Bearer-ключом из окружения и телом
`{"model":"Openai/Gpt-oss-120b","prompt":"Text to tokenize","add_special_tokens":true}`.

## Ограничение допуска

[Правила курса, пункт 0-1](https://github.com/deeppavlov/agentic-course-itmo/blob/main/projects/README.md#0-1-модели)
требуют Intelligence Index не ниже DeepSeek Flash на момент выбора.
При проверке 2026-11-15 Artificial Analysis показывал для
[gpt-oss-120b High](https://artificialanalysis.ai/models/gpt-oss-120b) индекс 4,
для [DeepSeek V4.1 Flash 1017 Max](https://artificialanalysis.ai/models/deepseek-v4-1-flash-1017)
— 33, оба на шкале v5.0.1. У старых V4 Flash и V4.1 Flash страницы показывали N/A.
Это не подтверждает допуск GPT-OSS по текущему порогу; нужно уточнение курса
либо доступ к разрешённой модели. Настройки AIRI не приравниваются к профилю High
в рейтинге. Технические пробы можно продолжать независимо от допуска.
