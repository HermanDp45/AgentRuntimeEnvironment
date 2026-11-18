# Доступ к модели курса — 2026-11-18

Пользователь предоставил ключ и инструкцию шлюза DeepPavlov. Ключ использован
только в памяти процесса через скрытый ввод; в файлы результатов не записан.
Рабочий `.env` и удалённый сервер не изменялись.

## Проверка

GET `https://litellm-backend.deeppavlov.ai/v1/models` вернул HTTP 200 и одну модель:
`deepseek-ai/DeepSeek-V4.1-Flash-1017`. Список сохранён в `models.json`.

Две синтетические пробы `/v1/chat/completions` из существующего `probe.py`:

| Проба | Результат | Время | Входные / выходные токены |
|---|---|---|---|
| Ответить `OK` | HTTP 200, точный ответ | 5.935 с | 38 / 12 |
| Вызвать `get_service_port` для `orders` | HTTP 200, корректный JSON tool call | 3.091 с | 305 / 54 |

Инструмент не исполнялся. Исходные запросы и ответы — `chat-probe.json`.
Это проверка API, не полный БАФ и не измерение качества пары.
Параметры: temperature=0, max_tokens=2048, stream=false,
таймаут 60 с, без повторов; reasoning effort явно не задан.
По инструкции шлюза в реальных кампаниях возможны очереди и более длинное ожидание.
Проверка очереди и достаточности таймаута приложения пока не проводилась.

Обязательный `make test-ci` завершился с кодом 0: 93 unit, 16 infra,
7 UI (2 live UI пропущены), 11 smoke и аварийный lifecycle. Лог — `test-ci.txt`.
Этот CI использует mock LLM. Smoke JSONL и манифест —
`../baf/early/20261118T080527421607Z.*`.

## Воспроизведение

Сначала безопасно задать `LLM_API_KEY` в окружении. Из корня проекта:

```bash
UV_CACHE_DIR=.cache/uv \
LLM_PROVIDER_URL=https://litellm-backend.deeppavlov.ai \
LLM_PROBE_MODEL=deepseek-ai/DeepSeek-V4.1-Flash-1017 \
LLM_PROBE_MAX_TOKENS=2048 \
uv run --frozen --offline python services/llm-proxy/probe.py \
  results/2026-11-18-deepseek-api/chat-probe.json
```

Для приложения используется `LLM_MODEL` с тем же идентификатором.
Наш `LLM_PROVIDER_URL` задаётся **без `/v1`**: код добавляет
`/v1/chat/completions`. В OpenAI SDK из инструкции, напротив, base_url содержит `/v1`.

## Допуск и рейтинг

DeepSeek 4.1 Flash прямо разрешён
[пунктом 0-1 правил курса](https://github.com/deeppavlov/agentic-course-itmo/blob/main/projects/README.md#0-1-модели).
На 2026-11-18 [Artificial Analysis](https://artificialanalysis.ai/models/deepseek-v4-1-flash-1017)
показывал для DeepSeek V4.1 Flash 1017 (Max) Intelligence Index 33 на шкале v5.0.1.
Режим Max относится к рейтингу; равенство настроек выданного шлюза этому режиму
не проверено. Версия, параметры и сырые результаты полной кампании ещё должны
быть зафиксированы отдельно.
