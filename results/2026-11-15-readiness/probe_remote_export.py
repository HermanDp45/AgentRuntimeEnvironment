"""Проба экспорта через уже открытое SSH-соединение, без установки на сервере."""

import argparse
import hashlib
import json
import shlex
import subprocess
import tempfile
from pathlib import Path

from client.workdir import Workdir

REMOTE = """
import json, sys, urllib.request, urllib.error
config = json.load(sys.stdin)
headers = {'Authorization': 'Bearer ' + config['token']}
headers.update(config.get('headers', {}))
request = urllib.request.Request(
    'http://127.0.0.1:18766' + config['path'],
    data=config['data'].encode() if 'data' in config else None,
    method='PUT' if 'data' in config else 'GET',
    headers=headers,
)
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        result = {'status': response.status, 'body': response.read().decode()}
except urllib.error.HTTPError as error:
    result = {'status': error.code}
except urllib.error.URLError:
    result = {'status': 'disconnected'}
print(json.dumps(result))
"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("host")
    parser.add_argument("--control-path", required=True)
    parser.add_argument("--known-hosts", required=True)
    args = parser.parse_args()
    ssh = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={args.known_hosts}",
        "-S",
        args.control_path,
    ]

    def command(extra, **kwargs):
        return subprocess.run(
            [*ssh, *extra], check=True, text=True, capture_output=True, timeout=15, **kwargs
        ).stdout

    checks = []
    with tempfile.TemporaryDirectory(prefix="are-remote-probe-") as directory:
        root = Path(directory)
        (root / "note.txt").write_text("from-mac")
        (root / ".env").write_text("synthetic-canary")
        (root / "link").symlink_to(root / ".env")
        export = Workdir(root)
        forward = f"127.0.0.1:18766:127.0.0.1:{export.server_port}"

        def request(path="/note.txt", **kwargs):
            return json.loads(
                command(
                    [args.host, "python3", "-c", shlex.quote(REMOTE)],
                    input=json.dumps({"token": export.token, "path": path, **kwargs}),
                )
            )

        forwarded = False
        try:
            command(["-O", "forward", "-R", forward, args.host])
            forwarded = True
            assert request() == {"status": 200, "body": "from-mac"}
            checks.append("server_reads_mac")
            (root / "note.txt").write_text("client-edit")
            assert request()["body"] == "client-edit"
            checks.append("client_edit_visible")
            assert request(token="invalid")["status"] == 401
            assert request("/.env")["status"] == 403
            assert request("/link")["status"] == 403
            checks.append("auth_and_path_boundaries")
            headers = {"X-Workdir-Sha256": hashlib.sha256(b"client-edit").hexdigest()}
            assert request(data="from-server", headers=headers)["status"] == 200
            assert (root / "note.txt").read_text() == "from-server"
            checks.append("server_write_visible_on_mac")
            assert request(data="stale", headers=headers)["status"] == 412
            assert (root / "note.txt").read_text() == "from-server"
            checks.append("stale_write_rejected")
            command(["-O", "cancel", "-R", forward, args.host])
            forwarded = False
            assert request()["status"] == "disconnected"
            checks.append("disconnect_reported")
        finally:
            if forwarded:
                command(["-O", "cancel", "-R", forward, args.host])
            export.close()
    print(json.dumps({"checks": checks, "passed": True, "scope": "SSH + WebDAV; no sandbox mount"}))


if __name__ == "__main__":
    main()
