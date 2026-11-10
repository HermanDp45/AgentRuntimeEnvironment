"""Ограниченный WebDAV-экспорт выбранного каталога для локальной сессии."""

import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
from contextlib import contextmanager
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree as ET

DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW if os.name == "posix" else 0
FILE_LIMIT = 65536
DAV = "{DAV:}"
ET.register_namespace("d", "DAV:")


def allowed(name):
    lower = name.casefold()
    return (
        name not in ("", ".", "..")
        and not any(char in name for char in "\0\\/")
        and lower not in (".git", ".env")
        and not lower.startswith((".env.", "._are_"))
    )


class Conflict(Exception):
    pass


class Workdir(ThreadingHTTPServer):
    def __init__(self, folder):
        if os.name != "posix":
            raise OSError("Подключение папок поддерживается на macOS и Linux.")
        self.root = os.open(folder, DIRECTORY)
        self.device = os.fstat(self.root).st_dev
        self.token = secrets.token_urlsafe(32)
        self.write_lock = threading.Lock()
        try:
            super().__init__(("127.0.0.1", 0), Handler)
        except BaseException:
            os.close(self.root)
            raise
        self.thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05})
        self.thread.start()

    def close(self):
        self.shutdown()
        self.thread.join()
        self.server_close()
        os.close(self.root)

    @contextmanager
    def directory(self, parts, create=False):
        fd = os.dup(self.root)
        try:
            for name in parts:
                if create:
                    try:
                        os.mkdir(name, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(name, DIRECTORY, dir_fd=fd)
                os.close(fd)
                fd = child
                if os.fstat(fd).st_dev != self.device:
                    raise PermissionError
            yield fd
        finally:
            os.close(fd)

    def info(self, parent, name):
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if info.st_dev != self.device or not (
            stat.S_ISDIR(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1)
        ):
            raise PermissionError
        return info

    @contextmanager
    def file(self, parent, name):
        handle = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=parent)
        with os.fdopen(handle, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_dev != self.device:
                raise PermissionError
            yield stream, info

    def replace(self, parts, data, expected):
        def current(fd):
            try:
                with self.file(fd, parts[-1]) as (stream, info):
                    previous = stream.read(FILE_LIMIT + 1)
                if len(previous) > FILE_LIMIT:
                    raise Conflict
                return hashlib.sha256(previous).hexdigest(), stat.S_IMODE(info.st_mode) & 0o777
            except FileNotFoundError:
                return "missing", 0o644

        with self.write_lock, self.directory(parts[:-1], create=expected == "missing") as fd:
            before, mode = current(fd)
            if before != expected:
                raise Conflict
            stage = "._are_" + secrets.token_hex(12)
            handle = os.open(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode, dir_fd=fd)
            try:
                with os.fdopen(handle, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fchmod(stream.fileno(), mode)
                    os.fsync(stream.fileno())
                if current(fd)[0] != expected:
                    raise Conflict
                if expected == "missing":
                    os.link(stage, parts[-1], src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
                else:
                    os.replace(stage, parts[-1], src_dir_fd=fd, dst_dir_fd=fd)
            finally:
                try:
                    os.unlink(stage, dir_fd=fd)
                except FileNotFoundError:
                    pass


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(5)
        super().setup()

    def log_message(self, *_):
        pass

    def reply(self, status, body=b"", headers=None):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def dispatch(self):
        auth = self.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(auth, f"Bearer {self.server.token}".encode()):
            self.reply(401)
            return
        try:
            path = unquote(urlsplit(self.path).path, errors="strict")
            parts = path.strip("/").split("/") if path.strip("/") else []
            if not all(map(allowed, parts)):
                self.reply(403)
            elif self.command == "OPTIONS":
                self.reply(200, headers={"DAV": "1", "Allow": "OPTIONS, GET, HEAD, PROPFIND, PUT"})
            elif self.command == "PROPFIND":
                self.propfind(parts)
            elif self.command == "PUT" and parts:
                self.put(parts)
            elif self.command in ("GET", "HEAD") and parts:
                self.download(parts)
            elif self.command == "GET" and not parts:
                self.reply(200, b"are-workdir-v1")
            else:
                self.reply(405)
        except Conflict:
            self.reply(412)
        except (FileNotFoundError, NotADirectoryError):
            self.reply(404)
        except OSError as error:
            if error.errno not in (errno.EPIPE, errno.ECONNRESET):
                self.reply(403 if error.errno in (None, errno.EACCES, errno.ELOOP) else 409)
        except ValueError:
            self.reply(400)

    do_OPTIONS = do_PROPFIND = do_GET = do_HEAD = do_PUT = dispatch

    def propfind(self, parts):
        depth = self.headers.get("Depth", "0")
        if depth not in ("0", "1"):
            self.reply(403)
            return
        listing = ET.Element(DAV + "multistatus")

        def append(path, info):
            node = ET.SubElement(listing, DAV + "response")
            directory = stat.S_ISDIR(info.st_mode)
            href = "/" + quote("/".join(path))
            ET.SubElement(node, DAV + "href").text = href + ("/" if directory and path else "")
            propstat = ET.SubElement(node, DAV + "propstat")
            prop = ET.SubElement(propstat, DAV + "prop")
            resource = ET.SubElement(prop, DAV + "resourcetype")
            if directory:
                ET.SubElement(resource, DAV + "collection")
            else:
                ET.SubElement(prop, DAV + "getcontentlength").text = str(info.st_size)
            ET.SubElement(prop, DAV + "getlastmodified").text = formatdate(
                info.st_mtime, usegmt=True
            )
            ET.SubElement(propstat, DAV + "status").text = "HTTP/1.1 200 OK"

        with self.server.directory(parts[:-1]) as fd:
            info = self.server.info(fd, parts[-1]) if parts else os.fstat(fd)
        append(parts, info)
        if depth == "1" and stat.S_ISDIR(info.st_mode):
            with self.server.directory(parts) as fd:
                with os.scandir(fd) as entries:
                    for entry in entries:
                        if allowed(entry.name):
                            try:
                                append([*parts, entry.name], self.server.info(fd, entry.name))
                            except (PermissionError, FileNotFoundError):
                                continue
        self.reply(207, ET.tostring(listing, encoding="utf-8"), {"Content-Type": "application/xml"})

    def download(self, parts):
        with (
            self.server.directory(parts[:-1]) as fd,
            self.server.file(fd, parts[-1]) as (file, info),
        ):
            start, end = 0, info.st_size - 1
            headers = {
                "Accept-Ranges": "bytes",
                "Last-Modified": formatdate(info.st_mtime, usegmt=True),
            }
            if value := self.headers.get("Range"):
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", value)
                if not match or int(match[1]) >= info.st_size:
                    self.reply(416, headers={"Content-Range": f"bytes */{info.st_size}"})
                    return
                start = int(match[1])
                end = min(end, int(match[2])) if match[2] else end
                if end < start:
                    self.reply(416)
                    return
                headers["Content-Range"] = f"bytes {start}-{end}/{info.st_size}"
            remaining = end - start + 1
            self.send_response(206 if value else 200)
            self.send_header("Content-Length", str(remaining))
            for key, header in headers.items():
                self.send_header(key, header)
            self.end_headers()
            if self.command != "HEAD":
                file.seek(start)
                while remaining and (chunk := file.read(min(remaining, 65536))):
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    def put(self, parts):
        expected = self.headers.get("X-Workdir-Sha256", "")
        if not re.fullmatch(r"missing|[0-9a-f]{64}", expected):
            self.reply(428)
            return
        length = int(self.headers.get("Content-Length", "-1"))
        if not 0 <= length <= FILE_LIMIT or self.headers.get("Transfer-Encoding"):
            self.reply(413)
            return
        content = self.rfile.read(length)
        if len(content) != length:
            self.reply(400)
            return
        self.server.replace(parts, content, expected)
        self.reply(
            200,
            json.dumps(
                {"sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}
            ).encode(),
            {"Content-Type": "application/json"},
        )
