# Причина сбоя DNS — 2026-11-06

Обновление: предложенная настройка уже включена в основной Compose и
[проверена на реальном API без ручного IP](../2026-11-06-dns-fix/README.md).
Ниже сохранены условия первоначальной диагностики.

## Вывод

В Docker daemon принудительно указаны 1.1.1.1 и 8.8.8.8. Обычные DNS-запросы
по UDP/53 к обоим адресам в текущей сети не получают ответа. Это воспроизводится
и с Mac, поэтому прежняя формулировка «DNS сломан только в Docker» была неточной.
По TCP/53 те же серверы отвечают, имя провайдера существует.

Контейнер по умолчанию использует UDP и получает EAI_AGAIN. В идентичном контейнере
с одной дополнительной опцией use-vc системный getaddrinfo разрешает имя, затем
HTTPS по имени достигает провайдера с включённой проверкой TLS — без extra_hosts
и без ручного IP провайдера. Контроль через Compose также прошёл.

Это подтверждённая непосредственная причина. На Mac активен VPN-маршрут utun4,
обнаружены AmneziaVPN-service и vpnagentd, но какой компонент теряет UDP-пакеты
(VPN, маршрутизатор или внешняя сеть), этими проверками не установлено.
VPN не отключался, глобальные настройки Docker и Mac не менялись.

## Доказательства

- configuration.json — только относящиеся к делу настройки, без аккаунтов/ключей.
- host.json и container.json — DNS по UDP/TCP, системное разрешение, контроль HTTPS/DoH.
- host-long-timeout.json — публичный UDP не ответил и за 10 секунд; TCP ответил
  за 0.079 с / 0.262 с. DNS локального роутера по UDP ответил за 0.024 с.
- control.json — одинаковые контейнеры в одной временной сети:
  default: gaierror; tcp_dns: адрес найден и HTTPS 404; desktop_dns: также успешно.
  Последний использует 192.168.65.7 — DNS текущего Docker Desktop, не универсальный адрес.
- compose.txt — use-vc проверен через настоящий Docker Compose, получены адрес и HTTPS 404.
- unit.txt — make test-ci: lock/Ruff, 42 passed, 9 skipped без инфраструктурного стенда.
- cleanup.txt — временная Compose-сеть удалена; остальные диагностические контейнеры/сети
  удалены в finally оркестратора.

HTTP 404 на корень inference.airi.net здесь означает успешные DNS, TCP, TLS и HTTP:
маршрут / не является модельным endpoint. Запросов с API-ключом не было.
В /etc/hosts Mac нет записи провайдера. Детальный механизм успешного системного
разрешения Mac (кеш/выбор scoped DNS/повтор транспорта) не трассировался.

В bare Python на Mac дополнительные SSL-пробы дали SSLCertVerificationError;
это отдельное ограничение его CA-хранилища, известное по предыдущей проверке API.
Оно не используется в выводе о DNS. В контейнере TLS прошёл; основной проект
использует httpx2 с системным trust store. Проверка сертификатов нигде не отключалась.

## Проверенное предложение исправления

Для llm-proxy достаточно dns_opt: [use-vc]. Это заставляет glibc выполнять DNS по TCP,
сохраняя текущие DNS-серверы. Привязка к старому IP провайдера больше не нужна.
Предложение сохранено в dns-tcp.yml; основной docker-compose.yml пока не менялся.
Сам факт переключения VPN не был частью разрешённого эксперимента.

Основание: [dns_opt в Compose](https://docs.docker.com/reference/compose-file/services/#dns_opt),
[use-vc в resolv.conf](https://man7.org/linux/man-pages/man5/resolv.conf.5.html),
[сеть Docker Desktop](https://docs.docker.com/desktop/features/networking/),
[контроль DNS over HTTPS](https://developers.cloudflare.com/1.1.1.1/encryption/dns-over-https/make-api-requests/).

## Воспроизведение

Публичные DNS-пробы на Mac без API-ключа:

```bash
python3 results/2026-11-06-dns-investigation/probe.py --timeout 10
```

Для контейнерного запуска тот же probe.py передавался через stdin в закреплённый
python:3.13.15-slim-bookworm, на отдельной временной bridge-сети, UID 10001,
read-only root, cap_drop ALL, no-new-privileges. Исходники и параметры находятся
в probe.py и control.json; ручной IP использовался только в отдельной TLS-пробе,
не при системном разрешении имени.

Та же пакетная проба в отдельной сети Docker (после запуска удалить сеть):

```bash
DNS_PROBE_IMAGE='python:3.13.15-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26'
docker network create --label are.purpose=dns-diagnostic are-dns-repeat
docker run --rm -i --network are-dns-repeat --read-only --cap-drop ALL --security-opt no-new-privileges:true --user 10001:10001 "$DNS_PROBE_IMAGE" python - --container < results/2026-11-06-dns-investigation/probe.py
docker network rm are-dns-repeat
```

Для контрольных вариантов к docker run перед именем образа добавлялись
--dns-option use-vc или --dns 192.168.65.7. Они не изменяют остальные контейнеры.

Контроль предлагаемой настройки средствами Compose:

```bash
docker compose -p are-dns-investigation -f results/2026-11-06-dns-investigation/compose.yml run --rm dns-probe
docker compose -p are-dns-investigation -f results/2026-11-06-dns-investigation/compose.yml down -v --remove-orphans
```

Это диагностика в текущей сети с активным VPN, не универсальный замер DNS-серверов.
