#!/bin/sh
# Эксперимент на Linux-хосте: WebDAV с loopback-порта SSH-туннеля.
set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 3 ]; then
    echo "Usage: sh mount-workdir.sh ABSOLUTE_EMPTY_DIR [port] [rw|ro]" >&2
    exit 2
fi
dir=$1
port=${2:-18766}
mode=${3:-rw}
case "$dir" in /*) ;; *) exit 2 ;; esac
case "$port" in ''|*[!0-9]*) exit 2 ;; esac
[ "$port" -ge 1 ] && [ "$port" -le 65535 ] || exit 2
case "$mode" in rw|ro) ;; *) exit 2 ;; esac
if [ ! -d "$dir" ] || [ -L "$dir" ] || mountpoint -q "$dir" || [ -n "$(ls -A "$dir")" ]; then
    echo "Mount point must be an empty, unmounted directory" >&2
    exit 2
fi
[ "$(rclone version | head -n 1)" = "rclone v1.75.2" ] || {
    echo "rclone v1.75.2 is required" >&2
    exit 2
}

if [ -z "${ARE_WEBDAV_PASSWORD:-}" ]; then
    terminal=$(stty -g)
    trap 'stty "$terminal"' EXIT
    trap 'exit 130' INT TERM HUP
    printf 'Временный пароль WebDAV: ' >&2
    stty -echo
    IFS= read -r ARE_WEBDAV_PASSWORD
    stty "$terminal"
    trap - EXIT INT TERM HUP
    printf '\n' >&2
fi
[ -n "$ARE_WEBDAV_PASSWORD" ] || exit 2

# Только именованные параметры эксперимента, без пользовательского rclone.conf.
RCLONE_CONFIG_WORKDIR_PASS=$(printf '%s\n' "$ARE_WEBDAV_PASSWORD" | rclone obscure -)
unset ARE_WEBDAV_PASSWORD
set -- mount workdir: "$dir" --config /dev/null \
    --allow-other --default-permissions --uid 10001 --gid 10001 \
    --dir-perms 0700 --file-perms 0600 \
    --vfs-cache-mode off --dir-cache-time 0s --attr-timeout 0s --poll-interval 0 \
    --contimeout 2s --timeout 5s --retries 1 --low-level-retries 1
if [ "$mode" = ro ]; then
    set -- "$@" --read-only
fi
exec env -i PATH="$PATH" HOME="${HOME:-/tmp}" \
    RCLONE_CONFIG_WORKDIR_TYPE=webdav \
    RCLONE_CONFIG_WORKDIR_URL="http://127.0.0.1:$port/" \
    RCLONE_CONFIG_WORKDIR_VENDOR=other \
    RCLONE_CONFIG_WORKDIR_USER=workdir \
    RCLONE_CONFIG_WORKDIR_PASS="$RCLONE_CONFIG_WORKDIR_PASS" \
    rclone "$@"
