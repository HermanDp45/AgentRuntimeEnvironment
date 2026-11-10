# Учебное монтирование workdir

Клиент создаёт временную папку с note.txt и отдаёт её через rclone WebDAV.
SSH переносит локальный WebDAV-порт на loopback Linux. Linux запускает rclone/FUSE
и показывает ту же папку по локальному пути. Для контейнера этот путь можно
подключить как /work. [Решение и ограничения](adr/0002-workdir-mount.md).

## Запуск на двух машинах

На компьютере пользователя нужны Python, rclone 1.75.2 и SSH-клиент.
На Linux нужны rclone 1.75.2, FUSE 3, OpenSSH-сервер с разрешённым обратным
пробросом и `GatewayPorts no`, а также Docker для проверки контейнера. Для FUSE
`allow_other` нужны соответствующие настройки на Linux. В примере SSH-хост
заранее добавлен в known_hosts, а порты 18765 и 18766 свободны.
[Сборки rclone](https://rclone.org/downloads/) · [Документация FUSE mount](https://rclone.org/commands/rclone_mount/).

На клиенте из корня проекта:

```bash
python -m client.workdir_demo --ssh user@linux-host
```

Введи случайный временный пароль WebDAV. Скрипт покажет путь к папке, в которой можно
открыть note.txt. Туннель слушает 127.0.0.1:18766 на Linux. Для режима чтения
добавь `--read-only`.

На Linux из корня проекта:

```bash
mount_dir=$(mktemp -d)
printf '%s\n' "$mount_dir"
sh services/sandbox-manager/mount-workdir.sh "$mount_dir" 18766 rw
```

Введи тот же пароль. Для режима чтения передай `ro` вместо `rw`.
На отдельной Linux-машине проверка bind mount запускается в другом терминале:

```bash
mount_dir=/абсолютный/путь/из/предыдущего/терминала
mountpoint -q "$mount_dir" && docker run --rm --network none --cap-drop ALL \
  --security-opt no-new-privileges --read-only --user 10001:10001 \
  --mount "type=bind,src=$mount_dir,dst=/work" \
  --entrypoint sh agentruntimeenvironment-sandbox:v0 -ec 'cat /work/note.txt'
```

Образ сандбокса создаётся через `make up` на Linux. Смонтированные файлы
доступны UID 10001. При завершении сначала останови тестовые контейнеры,
затем mount и клиентский экспорт через Ctrl+C. Пустой каталог монтирования
на Linux после размонтирования можно убрать через `rmdir`.

## Граница эксперимента

CLI создаёт свою папку. До экспорта настоящих проектов нужно обеспечить защиту
.git и симлинков на стороне клиента. Проверки выполнены на Mac и Linux внутри
Docker Desktop. Отдельная Linux-машина и Windows остаются для следующей проверки.
В чат и менеджер сандбоксов этот режим пока не подключён.
[Доказательства и команды тестов](../results/2026-11-11-webdav/README.md).
