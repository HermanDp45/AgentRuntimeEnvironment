"""Настоящие Qt-виджеты против управляемого HTTP/SSE-транспорта без модели."""

import json
import os
import threading
import time
from collections import deque

import httpx2
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6", reason="Install GUI dependencies: make setup-gui")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from client import api  # noqa: E402
from client.gui import ChatWindow  # noqa: E402


class EventStream(httpx2.SyncByteStream):
    def __init__(self, server):
        self.server = server

    def __iter__(self):
        while True:
            with self.server.condition:
                self.server.condition.wait_for(lambda: self.server.queue, timeout=0.05)
                event = self.server.queue.popleft() if self.server.queue else None
            if event is None:
                yield b": keepalive\n\n"
            else:
                yield ("data: " + json.dumps(event) + "\n\n").encode()
                if event["type"] == "run.result":
                    return


class Server:
    def __init__(self):
        self.condition = threading.Condition()
        self.queue = deque()
        self.requests = []
        self.sequence = 0
        self.files = {"note.txt": {"content": "original", "sha256": "fixture"}}
        self.closed = False
        self.messages = []
        self.interrupt_once = False
        self.resumes = []

    def emit(self, kind, data):
        self.sequence += 1
        event = {"type": "run.event", "sequence": self.sequence, "event": kind, "data": data}
        with self.condition:
            self.queue.append(event)
            self.condition.notify_all()

    def finish(self, status="ok"):
        self.sequence += 1
        with self.condition:
            self.queue.append(
                {
                    "type": "run.result",
                    "sequence": self.sequence,
                    "result": {"status": status, "value": {}},
                }
            )
            self.condition.notify_all()

    def handle(self, request):
        assert request.headers["Authorization"] == "Bearer test-client-token"
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, path, body))
        if path.endswith("/events"):
            self.resumes.append(int(request.url.params["after"]))
            if self.interrupt_once:
                self.interrupt_once = False
                return httpx2.Response(
                    200,
                    text='data: {"type":"run.event","sequence":1,'
                    '"event":"message","data":{"text":"Первый ответ"}}\n\n',
                )
            return httpx2.Response(200, stream=EventStream(self))
        if path == "/v0/tasks":
            self.messages.append(body["message"])
            self.sequence = 0
            if body["message"] == "history":
                self.emit("message", {"text": " | ".join(self.messages)})
                self.finish()
            elif body["message"] == "reconnect":
                self.interrupt_once = True
                self.sequence = 1
                self.finish()
            else:
                self.emit("status", {"phase": "model_reply", "model_call": 1, "duration_s": 0.1})
                self.emit(
                    "approval_required",
                    {
                        "operation_id": "op1",
                        "operation": {"kind": "WriteFile", "path": "note.txt"},
                        "diff": "--- note.txt\n+++ note.txt\n-original\n+changed",
                    },
                )
            return httpx2.Response(202, json={})
        if path == "/v0/approvals":
            if body["approved"]:
                self.files["note.txt"]["content"] = "changed"
            self.emit("message", {"text": "Готово"})
            self.finish()
        if path == "/v0/cancel":
            self.finish("cancelled")
            return httpx2.Response(
                200, json={"state": "finished", "result": {"status": "cancelled"}}
            )
        if path == "/v0/close":
            self.closed = True
            self.finish("cancelled")
        if path.endswith("/files"):
            return httpx2.Response(200, json={"files": self.files})
        return httpx2.Response(200, json={})


@pytest.fixture(scope="session")
def qt_app():
    return QApplication.instance() or QApplication([])


def wait(qt_app, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt_app.processEvents()
        if predicate():
            return
        QTest.qWait(10)
    assert predicate(), "GUI did not reach expected state"


@pytest.fixture
def chat(qt_app, monkeypatch):
    server = Server()
    monkeypatch.setattr(
        api,
        "connect",
        lambda url, token: httpx2.Client(
            base_url=url,
            headers={"Authorization": "Bearer " + token},
            transport=httpx2.MockTransport(lambda request: server.handle(request)),
        ),
    )
    window = ChatWindow(token="test-client-token")
    window.show()
    yield window, server
    window.close()
    wait(qt_app, lambda: not window.isVisible() and not window.jobs and not window.stream)
    assert server.closed or window.client is None


def connect(qt_app, window):
    QTest.mouseClick(window.connect_button, Qt.MouseButton.LeftButton)
    wait(qt_app, window.send_button.isEnabled)


def send(window, message):
    window.message.setPlainText(message)
    QTest.mouseClick(window.send_button, Qt.MouseButton.LeftButton)


@pytest.mark.parametrize("approved", [True, False])
def test_preview_approval_history_and_cleanup(qt_app, chat, approved):
    window, server = chat
    connect(qt_app, window)
    assert window.file_content.toPlainText() == "original"
    send(window, "edit")
    wait(qt_app, window.approval.isVisible)
    assert "-original" in window.preview.toPlainText()
    assert server.files["note.txt"]["content"] == "original"
    assert not window.send_button.isEnabled()
    assert "Ответ модели" in window.activity.toPlainText()
    QTest.mouseClick(window.allow if approved else window.deny, Qt.MouseButton.LeftButton)
    wait(qt_app, window.send_button.isEnabled)
    assert window.chat.toPlainText().count("Готово") == 1
    assert window.file_content.toPlainText() == ("changed" if approved else "original")
    assert sum(path == "/v0/approvals" for _, path, _ in server.requests) == 1
    session = window.session
    send(window, "history")
    wait(qt_app, window.send_button.isEnabled)
    assert window.session == session and "edit | history" in window.chat.toPlainText()


def test_cancel_waiting_and_close_active(qt_app, chat):
    window, server = chat
    connect(qt_app, window)
    send(window, "edit")
    wait(qt_app, window.approval.isVisible)
    QTest.mouseClick(window.cancel_button, Qt.MouseButton.LeftButton)
    wait(qt_app, window.send_button.isEnabled)
    assert "отменена" in window.status.text()
    assert server.files["note.txt"]["content"] == "original"
    send(window, "edit")
    wait(qt_app, window.approval.isVisible)
    window.close()
    wait(qt_app, lambda: not window.isVisible())
    assert server.closed and not window.jobs and not window.stream


def test_stream_reconnect_resumes_without_reposting(qt_app, chat):
    window, server = chat
    connect(qt_app, window)
    send(window, "reconnect")
    wait(qt_app, window.send_button.isEnabled)
    assert server.resumes == [0, 1]
    assert window.chat.toPlainText().count("Первый ответ") == 1
    assert sum(path == "/v0/tasks" for _, path, _ in server.requests) == 1


def test_http_error_does_not_display_token(qt_app, chat, monkeypatch):
    window, server = chat
    original = server.handle
    monkeypatch.setattr(
        server,
        "handle",
        lambda request: (
            original(request)
            if request.url.path == "/v0/close"
            else httpx2.Response(
                401,
                json={"detail": "test-client-token"},
                request=request,
            )
        ),
    )
    QTest.mouseClick(window.connect_button, Qt.MouseButton.LeftButton)
    wait(qt_app, lambda: "отклонил" in window.status.text())
    assert "test-client-token" not in window.status.text() + window.activity.toPlainText()
    assert not window.send_button.isEnabled()
    assert window.client is None and window.connect_button.isEnabled()
    monkeypatch.setattr(server, "handle", original)
    connect(qt_app, window)


def test_close_failure_keeps_window_until_retry(qt_app, chat, monkeypatch):
    window, server = chat
    connect(qt_app, window)
    original = server.handle
    failed = False

    def handle(request):
        nonlocal failed
        if request.url.path == "/v0/close" and not failed:
            failed = True
            return httpx2.Response(503)
        return original(request)

    monkeypatch.setattr(server, "handle", handle)
    window.close()
    wait(qt_app, lambda: "не подтверждено" in window.status.text())
    assert window.isVisible() and not server.closed
    window.close()
    wait(qt_app, lambda: not window.isVisible())
    assert server.closed


def test_lost_task_response_observes_same_run(qt_app, chat, monkeypatch):
    window, server = chat
    connect(qt_app, window)
    original = server.handle

    def handle(request):
        response = original(request)
        if request.url.path == "/v0/tasks":
            raise httpx2.ReadTimeout("lost response", request=request)
        return response

    monkeypatch.setattr(server, "handle", handle)
    send(window, "edit")
    wait(qt_app, window.approval.isVisible)
    assert sum(path == "/v0/tasks" for _, path, _ in server.requests) == 1
    assert server.files["note.txt"]["content"] == "original"
    QTest.mouseClick(window.deny, Qt.MouseButton.LeftButton)
    wait(qt_app, window.send_button.isEnabled)


@pytest.mark.docker
@pytest.mark.skipif(not os.getenv("HARNESS_TEST_URL"), reason="Use make up-test")
def test_gui_over_http_with_real_sandbox(qt_app):
    window = ChatWindow(os.environ["HARNESS_TEST_URL"], "fixture-client-token")
    window.show()
    try:
        connect(qt_app, window)
        send(window, "test:edit")
        wait(qt_app, window.approval.isVisible, timeout=20)
        assert "-original" in window.preview.toPlainText()
        assert window.file_content.toPlainText() == "original"
        first_operation = window.pending
        QTest.mouseClick(window.allow, Qt.MouseButton.LeftButton)
        wait(qt_app, lambda: window.pending and window.pending != first_operation, timeout=20)
        assert "exit 1" in window.preview.toPlainText()
        QTest.mouseClick(window.allow, Qt.MouseButton.LeftButton)
        wait(qt_app, window.send_button.isEnabled, timeout=20)
        assert window.file_content.toPlainText() == "changed"
        assert "кодом 1" in window.chat.toPlainText()
        assert "output" in window.activity.toPlainText()
        output = os.getenv("GUI_SCREENSHOT")
        if output:
            assert window.grab().save(output)
    finally:
        window.close()
        wait(
            qt_app,
            lambda: not window.isVisible() and not window.jobs and not window.stream,
            timeout=25,
        )
