# Исправление DNS прокси — 2026-11-06

В основном docker-compose.yml у llm-proxy добавлен dns_opt: [use-vc].
Остальные сервисы, глобальные настройки Docker и VPN не менялись.
Причина выбора — [проверка UDP/TCP](../2026-11-06-dns-investigation/README.md).
Ручной IP провайдера и прежний DNS-overlay не используются.

## Проверка

```bash
make test-ci
make up
uv run --frozen --offline python services/harness/probe.py results/dns-read.json
make up-test
make test-agent
make down-test
make down
```

- unit.txt — lock/Ruff, 42 passed, 9 явно пропущенных инфраструктурных сценариев.
- build.txt — обычный сервер из исходников, healthy всех компонентов.
- resolution.json — DNS через TCP есть в resolv.conf, записи провайдера в hosts нет;
  HTTPS по имени с проверкой сертификата отвечает 404 на корневой маршрут.
- live-read.json — Qwen через gateway/харнес/proxy/сандбокс прочитала PORT=7319
  и ответила 7319, два обращения, 2.446 с. Это функциональная проба, не замер скорости.
- build-offline.txt, infra.txt — полный offline-стенд с той же DNS-опцией: 9 passed.
- cleanup.txt — остановка обоих стендов, отсутствие их контейнеров и сандбоксов.

Внешний запрос с ключом выполнял только LLM-прокси. Данные синтетические,
ключ и содержимое пользовательских папок в результаты не включались.

Linux CI на 449b8d6 также прошёл: python и compose, включая 9 offline-сценариев
и очистку. [Ручной запуск 42240257435](https://github.com/HermanDp45/AgentRuntimeEnvironment/actions/runs/42240257435), исходный результат — ci.json.
