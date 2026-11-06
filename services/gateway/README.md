# Gateway

Работают GET /health и передача /v0/* в харнес с проверкой Bearer-токена.
Gateway пересылает HTTP-ответы и SSE, включая Last-Event-ID; ключ модели не получает.
Типы запросов проверяет харнес по contracts/v0.py.

В Compose HARNESS_URL указывает на внутренний сервис harness. CLIENT_TOKENS — список
клиентских токенов через запятую. Для локальной разработки Compose использует
local-dev-token, если значение пустое. Снаружи открыт только loopback-порт 8080;
для удалённого развёртывания нужны собственный токен и защищённый транспорт.

Исходники — src/agent_runtime/gateway.py, Dockerfile — в этом каталоге.
make run поднимает только gateway; без доступного HARNESS_URL его /v0/* не работают.
Полный сервер — make up. [Протокол](../../contracts/README.md),
[клиент](../../client/README.md), [структура](../../docs/adr/0003-course-template.md).
