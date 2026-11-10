"""Том с локальным WebDAV: сеть есть только у доверенного помощника."""

import json
import time
from urllib.parse import quote

import docker
import httpx2

from contracts.v0 import Failed, Success
from sandbox_manager.runtime import Rejected

IMAGE = (
    "rclone/rclone:1.75.2@sha256:2687085f718d3c628f7fdfb77c52a1d344332aed543110bb88088f2c70d43eb5"
)
HOST = "host.docker.internal"


class MountedWorkdir:
    def __init__(self, docker_client, request, namespace):
        self.endpoint = f"http://{HOST}:{request.port}"
        self.headers = {"Authorization": "Bearer " + request.export_token}
        self.network = self.helper = self.volume = None
        name = f"are-{namespace}-mount-{request.session_id.hex}"
        labels = {"are.owner": namespace, "are.mount": "true"}
        try:
            with self.http() as client:
                response = client.get("/")
                if response.status_code != 200 or response.content != b"are-workdir-v1":
                    raise Rejected("workdir_unavailable", 503)
        except httpx2.HTTPError:
            raise Rejected("workdir_unavailable", 503) from None
        try:
            try:
                docker_client.images.get(IMAGE)
            except docker.errors.ImageNotFound:
                raise Rejected("workdir_image_missing", 503) from None
            self.network = docker_client.networks.create(name, driver="bridge", labels=labels)
            self.helper = docker_client.containers.run(
                IMAGE,
                [
                    "serve",
                    "nfs",
                    "folder:",
                    "--addr",
                    ":2049",
                    "--read-only",
                    "--dir-cache-time",
                    "5m",
                    "--rc",
                    "--rc-addr",
                    "127.0.0.1:5572",
                    "--rc-no-auth",
                    "--poll-interval",
                    "0",
                    "--contimeout",
                    "2s",
                    "--timeout",
                    "5s",
                    "--retries",
                    "1",
                    "--low-level-retries",
                    "1",
                    "--vfs-cache-mode",
                    "off",
                    "--nfs-cache-type",
                    "disk",
                    "--cache-dir",
                    "/tmp",
                    "--uid",
                    "10001",
                    "--gid",
                    "10001",
                ],
                detach=True,
                name=name,
                network=name,
                labels=labels,
                extra_hosts={HOST: "host-gateway"},
                read_only=True,
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true"],
                mem_limit="128m",
                pids_limit=64,
                tmpfs={"/tmp": "rw,nosuid,nodev,noexec,size=8m"},
                environment={
                    "RCLONE_CONFIG_FOLDER_TYPE": "webdav",
                    "RCLONE_CONFIG_FOLDER_URL": self.endpoint,
                    "RCLONE_CONFIG_FOLDER_BEARER_TOKEN": request.export_token,
                    "RCLONE_CONFIG_FOLDER_VENDOR": "other",
                    "RCLONE_CONFIG": "/dev/null",
                },
            )
            for _ in range(12):
                if (
                    self.helper.exec_run(["nc", "-z", "-w", "1", "127.0.0.1", "2049"]).exit_code
                    == 0
                ):
                    break
                time.sleep(0.1)
            else:
                raise Rejected("workdir_mount_unavailable", 503)
            self.helper.reload()
            address = self.helper.attrs["NetworkSettings"]["Networks"][name]["IPAddress"]
            options = (
                f"addr={address},nfsvers=3,port=2049,mountport=2049,tcp,"
                "ro,soft,timeo=10,retrans=1,nosuid,nodev,actimeo=0,lookupcache=none"
            )
            self.volume = docker_client.volumes.create(
                name=name,
                driver="local",
                driver_opts={"type": "nfs", "o": options, "device": ":/"},
                labels=labels,
            )
        except Exception:
            self.close()
            raise

    def refresh(self):
        result = self.helper.exec_run(
            [
                "rclone",
                "rc",
                "--url",
                "http://127.0.0.1:5572",
                "vfs/refresh",
                "recursive=true",
            ]
        )
        if result.exit_code != 0:
            raise RuntimeError("Workdir refresh failed")
        refreshed = json.loads(result.output).get("result", {})
        if not refreshed or any(status != "OK" for status in refreshed.values()):
            raise RuntimeError("Workdir refresh failed")

    def http(self):
        return httpx2.Client(
            base_url=self.endpoint,
            headers=self.headers,
            timeout=5,
            follow_redirects=False,
            trust_env=False,
        )

    def close(self):
        for resource in (self.volume, self.helper, self.network):
            if resource is not None:
                try:
                    if resource is self.helper:
                        resource.remove(force=True)
                    else:
                        resource.remove()
                except docker.errors.NotFound:
                    pass

    def write(self, operation):
        try:
            with self.http() as client:
                result = client.put(
                    "/" + quote(operation.path),
                    content=operation.content.encode(),
                    headers={"X-Workdir-Sha256": operation.expected_sha256},
                )
            if result.status_code == 200:
                return Success(status="ok", value=result.json())
            code = {
                403: "invalid_path",
                404: "not_found",
                409: "operation_failed",
                412: "write_conflict",
                413: "file_too_large",
            }.get(result.status_code, "workdir_unavailable")
        except httpx2.HTTPError:
            code = "workdir_unavailable"
        kind = "transport" if code == "workdir_unavailable" else "sandbox"
        return Failed(status="error", error={"kind": kind, "code": code, "message": code})
