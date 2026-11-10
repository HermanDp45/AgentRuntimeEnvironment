"""Единственное место с доступом к Docker daemon."""

import json
import threading
from pathlib import Path

import docker

from contracts.v0 import Failed, MountedProvision, Success
from sandbox_manager.workdir import MountedWorkdir


class DockerBackend:
    def __init__(self, config, namespace):
        self.config, self.namespace = config, namespace
        self.client = docker.from_env(version="auto", timeout=10)
        self.image = self.client.images.get(config["image"]).id
        self.gate = threading.RLock()
        self.mounts = {}

    def health(self):
        self.client.ping()
        self.client.images.get(self.image)

    def cleanup(self):
        filters = {"label": f"are.owner={self.namespace}"}
        containers = self.client.containers.list(all=True, filters=filters)
        for container in sorted(containers, key=lambda item: "are.mount" in item.labels):
            container.remove(force=True)
        for volume in self.client.volumes.list(filters=filters):
            volume.remove()
        for network in self.client.networks.list(filters=filters):
            network.remove()

    def provision(self, request):
        config = self.config
        uid = 0 if request.read_only else 10001
        tmpfs = {
            "/tmp": f"rw,noexec,nosuid,nodev,size={config['tmp_bytes']},uid=10001,gid=10001",
        }
        mount = None
        mounts = []
        if isinstance(request, MountedProvision):
            mount = MountedWorkdir(self.client, request, self.namespace)
            mounts.append(
                docker.types.Mount(
                    "/work", mount.volume.name, type="volume", read_only=True, no_copy=True
                )
            )
        else:
            tmpfs["/work"] = (
                f"rw,nosuid,nodev,size={config['work_bytes']},uid={uid},gid={uid},mode=0755"
            )
        container = None
        try:
            container = self.client.containers.create(
                self.image,
                ["idle"],
                name=f"are-{self.namespace}-{request.session_id.hex}",
                user="10001:10001",
                detach=True,
                read_only=True,
                network_mode="none",
                cap_drop=["ALL"],
                security_opt=["no-new-privileges:true", "seccomp=builtin"],
                mem_limit=config["memory"],
                memswap_limit=config["memory"],
                nano_cpus=int(config["cpus"] * 1_000_000_000),
                pids_limit=config["pids"],
                labels={"are.owner": self.namespace, "are.session": str(request.session_id)},
                tmpfs=tmpfs,
                mounts=mounts,
            )
            container.start()
            if mount:
                self.mounts[container.id] = mount
                return container
            for path, content in request.files.items():
                result = self.worker(
                    container,
                    {
                        "kind": "WriteFile",
                        "path": path,
                        "content": content,
                        "expected_sha256": "missing",
                        "stage": "._are_provision.tmp",
                    },
                    user=str(uid),
                )
                if result.status != "ok":
                    raise ValueError(result.error.code)
            if request.read_only:
                result = self.worker(container, {"kind": "Freeze"}, user="0")
                if result.status != "ok":
                    raise ValueError(result.error.code)
            return container
        except Exception:
            if container is not None:
                container.remove(force=True)
            if mount:
                mount.close()
            raise

    def worker(
        self,
        container,
        payload,
        output=lambda *_: None,
        *,
        gate=None,
        cancelled=lambda: False,
        user="10001:10001",
    ):
        if payload["kind"] in ("ReadFile", "RunCommand", "Collect"):
            if mount := self.mounts.get(container.id):
                mount.refresh()
        payload = {
            **payload,
            "limit": self.config[
                "output_bytes" if payload["kind"] == "RunCommand" else "file_bytes"
            ],
        }
        encoded = json.dumps(payload, ensure_ascii=True)
        command = ["python", "/opt/sandbox_worker.py"]
        command.extend(encoded[index : index + 16000] for index in range(0, len(encoded), 16000))
        # Cancel и начало exec используют один gate: команда не стартует после отмены.
        with gate or self.gate:
            if cancelled():
                return None
            execution = self.client.api.exec_create(container.id, command, user=user)
            stream = self.client.api.exec_start(execution["Id"], stream=True, demux=True)
        buffer, result = b"", None
        for stdout, stderr in stream:
            if stderr:
                raise RuntimeError("Sandbox worker failed")
            buffer += stdout or b""
            if len(buffer) > 1024 * 1024:
                raise RuntimeError("Sandbox worker message exceeds its limit")
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                event = json.loads(line)
                if event["type"] == "result":
                    result = (
                        Success(status="ok", value=event["value"])
                        if event["ok"]
                        else Failed(status="error", value=event["value"], error=event["error"])
                    )
                else:
                    output(event["type"], event["text"])
        if result is None and not cancelled():
            raise RuntimeError("Sandbox worker exited without a result")
        return result

    def execute(self, container, request, output, gate, cancelled, backup_dir):
        operation = request.operation
        if operation.kind == "WriteFile" and (mount := self.mounts.get(container.id)):
            with gate:
                if cancelled():
                    return None
            return mount.write(operation)
        payload = operation.model_dump()
        stage = f"._are_{request.operation_id.hex}.tmp"
        backup = Path(backup_dir) / f"{request.operation_id}.bak"
        try:
            if operation.kind == "WriteFile":
                old = self.worker(
                    container,
                    {"kind": "ReadFile", "path": operation.path},
                    gate=gate,
                    cancelled=cancelled,
                )
                if old and old.status == "ok":
                    backup.write_text(old.value["content"], encoding="utf-8")
                    backup.chmod(0o600)
                payload["stage"] = stage
            return self.worker(container, payload, output, gate=gate, cancelled=cancelled)
        finally:
            # Потомки, включая setsid(), не должны переживать завершённую команду.
            try:
                with gate:
                    self.cancel(container)
                    if operation.kind == "WriteFile":
                        self.worker(
                            container,
                            {"kind": "CleanStage", "path": operation.path, "stage": stage},
                        )
            finally:
                backup.unlink(missing_ok=True)

    def cancel(self, container):
        result = self.worker(container, {"kind": "Kill"})
        if result.status != "ok":
            raise RuntimeError("Sandbox cancellation failed")

    def collect(self, container):
        return self.worker(container, {"kind": "Collect", "mounted": container.id in self.mounts})

    def release(self, container):
        try:
            container.remove(force=True)
        except docker.errors.NotFound:
            pass
        if mount := self.mounts.get(container.id):
            mount.close()
            del self.mounts[container.id]
