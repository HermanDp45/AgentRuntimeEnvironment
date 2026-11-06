"""Учебный чат: HTTP-команды и SSE, без доступа к папкам Mac."""

import argparse
import os
import sys
from uuid import uuid4

import httpx2
from PySide6.QtCore import QThread, QTimer, Signal, Slot
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from client import api


def error_message(error):
    if isinstance(error, httpx2.HTTPStatusError):
        code = error.response.status_code
        if code in (401, 403):
            return "Сервер отклонил токен клиента."
        return f"Сервер вернул HTTP {code}. Проверьте состояние сессии."
    if isinstance(error, httpx2.HTTPError):
        return "Нет связи с сервером. Проверьте адрес и запуск Compose."
    return "Некорректный ответ сервера. Подробности — в серверном журнале."


class Job(QThread):
    succeeded = Signal(object)
    failed = Signal(object)

    def __init__(self, action, parent):
        super().__init__(parent)
        self.action = action

    def run(self):
        try:
            self.succeeded.emit(self.action())
        except Exception as error:
            self.failed.emit(error)


class Stream(QThread):
    received = Signal(object)
    failed = Signal(str)
    reconnecting = Signal()

    def __init__(self, client, run, parent):
        super().__init__(parent)
        self.client, self.run_id = client, run

    def run(self):
        after = 0
        for attempt in range(3):
            try:
                for event in api.events(
                    self.client, self.run_id, after, self.isInterruptionRequested
                ):
                    if event["sequence"] <= after:
                        continue
                    after = event["sequence"]
                    self.received.emit(event)
                    if event["type"] == "run.result":
                        return
            except (httpx2.HTTPError, ValueError, KeyError):
                pass
            if self.isInterruptionRequested():
                return
            if attempt < 2:
                self.reconnecting.emit()
                # Повторяется только GET с последним sequence, не команда или задача.
                if self.wait_for_retry():
                    return
        self.failed.emit("Поток событий прерван. Отмените задачу или закройте чат.")

    def wait_for_retry(self):
        for _ in range(10):
            if self.isInterruptionRequested():
                return True
            self.msleep(100)
        return False


def text_view():
    view = QPlainTextEdit()
    view.setReadOnly(True)
    return view


class ChatWindow(QMainWindow):
    def __init__(self, url="http://127.0.0.1:8080", token=None):
        super().__init__()
        self.client = None
        self.session = self.run_id = self.pending = None
        self.jobs = set()
        self.stream = None
        self.closing = False
        self.close_done = False
        self.release_started = False
        self.last_files = {}
        self.setWindowTitle("Agent Runtime — учебный чат")
        self.resize(1080, 780)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 16, 18, 16)
        self.url = QLineEdit(url)
        self.url.setAccessibleName("Адрес сервера")
        self.token = QLineEdit(token or os.getenv("CLIENT_TOKEN", "local-dev-token"))
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setAccessibleName("Токен клиента")
        self.connect_button = QPushButton("Подключить чат")
        self.connect_button.clicked.connect(self.connect_chat)
        connection = QHBoxLayout()
        for widget in (
            QLabel("Сервер"),
            self.url,
            QLabel("Токен"),
            self.token,
            self.connect_button,
        ):
            connection.addWidget(widget)
        layout.addLayout(connection)
        banner = QLabel(
            "Учебная папка: note.txt = original. Папки Mac не подключены. "
            "При закрытии окна временные файлы удаляются."
        )
        banner.setWordWrap(True)
        layout.addWidget(banner)
        self.chat, self.activity, self.file_content = text_view(), text_view(), text_view()
        self.chat.setPlaceholderText("Подключите чат и отправьте задачу агенту.")
        self.activity.setMaximumBlockCount(2000)
        self.file_list = QListWidget()
        self.file_list.currentTextChanged.connect(self.show_file)
        files = QSplitter()
        files.addWidget(self.file_list)
        files.addWidget(self.file_content)
        files.setSizes([130, 300])
        tabs = QTabWidget()
        tabs.addTab(self.activity, "Действия")
        tabs.addTab(files, "Файлы")
        split = QSplitter()
        split.addWidget(self.chat)
        split.addWidget(tabs)
        split.setSizes([590, 430])
        layout.addWidget(split, 1)
        self.approval = QGroupBox("Нужно разрешение")
        approval_layout = QVBoxLayout(self.approval)
        self.operation = QLabel()
        self.preview = text_view()
        self.preview.setMaximumHeight(155)
        approval_layout.addWidget(self.operation)
        approval_layout.addWidget(self.preview)
        decisions = QHBoxLayout()
        self.allow, self.deny = QPushButton("Разрешить"), QPushButton("Отклонить")
        self.allow.clicked.connect(lambda: self.decide(True))
        self.deny.clicked.connect(lambda: self.decide(False))
        decisions.addStretch()
        decisions.addWidget(self.deny)
        decisions.addWidget(self.allow)
        approval_layout.addLayout(decisions)
        layout.addWidget(self.approval)
        self.approval.hide()
        self.message = QPlainTextEdit()
        self.message.setPlaceholderText("Например: прочитай note.txt и замени original на changed")
        self.message.setMaximumHeight(90)
        self.message.setAccessibleName("Сообщение агенту")
        layout.addWidget(self.message)
        actions = QHBoxLayout()
        self.status = QLabel("Нет подключения")
        self.status.setWordWrap(True)
        self.send_button, self.cancel_button = QPushButton("Отправить"), QPushButton("Отмена")
        self.send_button.clicked.connect(self.send)
        self.cancel_button.clicked.connect(self.cancel)
        actions.addWidget(self.status, 1)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.send_button)
        layout.addLayout(actions)
        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self.send)
        self.set_busy(False)

    def job(self, action, success, failure=None):
        worker = Job(action, self)
        self.jobs.add(worker)
        worker.succeeded.connect(success)
        worker.failed.connect(failure or self.show_error)
        worker.finished.connect(self.job_finished)
        worker.start()

    @Slot()
    def job_finished(self):
        worker = self.sender()
        self.jobs.discard(worker)
        worker.deleteLater()
        self.finish_close()

    @Slot(object)
    def show_error(self, message):
        if not isinstance(message, str):
            message = error_message(message)
        self.status.setText(message)
        self.activity.appendPlainText(message)

    def set_busy(self, busy):
        self.busy = busy
        self.send_button.setEnabled(
            bool(self.session) and not busy and not self.stream and not self.closing
        )
        self.cancel_button.setEnabled(bool(self.run_id) and not self.closing)

    @Slot()
    def connect_chat(self):
        if self.client or self.closing:
            return
        url = self.url.text().strip()
        try:
            parsed = httpx2.URL(url)
        except httpx2.InvalidURL:
            self.show_error("Некорректный адрес сервера.")
            return
        if parsed.scheme not in ("http", "https") or not parsed.host or parsed.userinfo:
            self.show_error("Укажите HTTP/HTTPS-адрес без логина и пароля в URL.")
            return
        self.client = api.connect(url, self.token.text())
        self.session = str(uuid4())
        self.url.setEnabled(False)
        self.token.setEnabled(False)
        self.connect_button.setEnabled(False)
        self.status.setText("Создаём учебную сессию…")
        self.job(
            lambda: api.post(
                self.client,
                "/v0/sessions",
                "session.fixture",
                session_id=self.session,
                files={"note.txt": "original"},
            ),
            self.connected,
            self.connection_failed,
        )

    @Slot(object)
    def connection_failed(self, error):
        if isinstance(error, httpx2.HTTPStatusError) and error.response.status_code in (401, 403):
            # Отказ авторизации однозначен: сессия не создавалась.
            self.client.close()
            self.client = self.session = None
            self.url.setEnabled(True)
            self.token.setEnabled(True)
            self.connect_button.setEnabled(True)
            self.show_error(error)
        else:
            # После неоднозначного POST сохраняем UUID для закрытия сессии.
            self.show_error(error_message(error) + " Закройте окно и подключитесь заново.")

    @Slot(object)
    def connected(self, _):
        if self.closing:
            return
        self.status.setText("Готов к задаче · учебная сессия")
        self.connect_button.setText("Подключено")
        self.set_busy(False)
        self.collect_files()
        self.message.setFocus()

    @Slot()
    def send(self):
        message = self.message.toPlainText().strip()
        if not self.send_button.isEnabled() or not message:
            return
        self.run_id = str(uuid4())
        self.chat.appendPlainText("Вы\n" + message + "\n")
        self.message.clear()
        self.set_busy(True)
        self.cancel_button.setEnabled(False)
        self.status.setText("Передаём задачу…")
        self.job(
            lambda: api.post(
                self.client,
                "/v0/tasks",
                "task",
                session_id=self.session,
                run_id=self.run_id,
                message=message,
            ),
            self.task_started,
            self.task_uncertain,
        )

    @Slot(object)
    def task_uncertain(self, error):
        self.show_error(
            error_message(error) + " Проверяем известный запуск; повторно задачу не отправляем."
        )
        self.task_started(None)

    @Slot(object)
    def task_started(self, _):
        if self.closing:
            return
        self.status.setText("Агент работает…")
        self.cancel_button.setEnabled(True)
        self.stream = Stream(self.client, self.run_id, self)
        self.stream.received.connect(self.on_event)
        self.stream.failed.connect(self.show_error)
        self.stream.reconnecting.connect(self.reconnecting)
        self.stream.finished.connect(self.stream_finished)
        self.stream.start()

    @Slot()
    def reconnecting(self):
        self.status.setText("Восстанавливаем поток событий…")

    @Slot()
    def stream_finished(self):
        self.sender().deleteLater()
        self.stream = None
        self.set_busy(self.busy)
        self.finish_close()

    @Slot(object)
    def on_event(self, event):
        if self.closing or event.get("run_id", self.run_id) != self.run_id:
            return
        if event["type"] == "run.result":
            if not self.run_id:
                return
            result = event["result"]
            labels = {
                "ok": "Задача завершена",
                "error": "Задача завершилась с ошибкой",
                "cancelled": "Задача отменена; завершённые правки сохранены",
            }
            self.status.setText(labels.get(result["status"], "Задача завершена"))
            if result.get("error"):
                self.activity.appendPlainText("Ошибка: " + result["error"]["code"])
            self.run_id = self.pending = None
            self.approval.hide()
            self.cancel_button.setEnabled(False)
            self.collect_files()
            return
        data = event["data"]
        kind = event["event"]
        if kind == "message":
            self.chat.appendPlainText("Агент\n" + data["text"] + "\n")
        elif kind == "approval_required":
            self.pending = data["operation_id"]
            operation = data["operation"]
            self.operation.setText(
                "Запись файла: " + operation["path"]
                if operation["kind"] == "WriteFile"
                else "Выполнение команды в сандбоксе"
            )
            self.preview.setPlainText(data.get("diff", operation.get("command", "")))
            self.allow.setEnabled(True)
            self.deny.setEnabled(True)
            self.approval.show()
            self.status.setText("Агент ждёт вашего разрешения")
        elif kind == "tool":
            phase = data.get("phase")
            if phase in ("stdout", "stderr"):
                self.activity.appendPlainText(data["text"].rstrip())
            elif phase == "requested":
                names = {
                    "ReadFile": "Чтение файла",
                    "WriteFile": "Запись файла",
                    "RunCommand": "Команда",
                }
                self.activity.appendPlainText(names.get(data["kind"], data["kind"]))
            elif phase == "executed":
                self.activity.appendPlainText(
                    f"Результат: {data['status']} · {data['duration_s']:.2f} с"
                    + (f" · exit {data['exit_code']}" if data.get("exit_code") is not None else "")
                )
        elif kind == "status" and data.get("phase") == "model_reply":
            self.activity.appendPlainText(
                f"Ответ модели #{data['model_call']} · {data['duration_s']:.2f} с"
            )

    @Slot()
    def collect_files(self):
        def collect():
            response = self.client.get(f"/v0/sessions/{self.session}/files")
            response.raise_for_status()
            return response.json()["files"]

        self.set_busy(True)
        self.job(collect, self.files_received, self.files_failed)

    @Slot(object)
    def files_received(self, files):
        if self.closing:
            return
        self.last_files = files
        self.file_list.clear()
        self.file_content.clear()
        self.file_list.addItems(sorted(files))
        self.file_list.setCurrentRow(0)
        self.set_busy(False)

    @Slot(object)
    def files_failed(self, message):
        self.show_error(message)
        self.set_busy(False)

    @Slot(str)
    def show_file(self, name):
        self.file_content.setPlainText(self.last_files.get(name, {}).get("content", ""))

    def decide(self, approved):
        if not self.pending or not self.allow.isEnabled():
            return
        operation = self.pending
        self.allow.setEnabled(False)
        self.deny.setEnabled(False)
        self.job(
            lambda: api.post(
                self.client,
                "/v0/approvals",
                "approval",
                session_id=self.session,
                run_id=self.run_id,
                operation_id=operation,
                approved=approved,
            ),
            lambda _: self.decided(operation),
            lambda error: self.approval_failed(operation, error),
        )

    def decided(self, operation):
        if self.pending == operation:
            self.pending = None
            self.approval.hide()
            self.status.setText("Агент работает…")

    def approval_failed(self, operation, message):
        self.show_error(message)
        if self.pending == operation:
            # Повтор того же approval идемпотентен; решение можно отправить снова.
            self.allow.setEnabled(True)
            self.deny.setEnabled(True)

    @Slot()
    def cancel(self):
        if not self.run_id:
            return
        run = self.run_id
        self.cancel_button.setEnabled(False)
        self.status.setText("Останавливаем задачу…")
        self.job(
            lambda: api.post(
                self.client, "/v0/cancel", "run.cancel", session_id=self.session, run_id=run
            ),
            lambda state: self.cancel_received(run, state),
            self.cancel_failed,
        )

    def cancel_received(self, run, state):
        if state.get("state") == "finished" and self.run_id == run:
            self.on_event({"type": "run.result", "run_id": run, "result": state["result"]})

    @Slot(object)
    def cancel_failed(self, message):
        self.show_error(message)
        self.cancel_button.setEnabled(bool(self.run_id) and not self.closing)

    def closeEvent(self, event):
        if self.close_done or not self.client:
            event.accept()
            return
        event.ignore()
        if self.closing:
            return
        self.closing = True
        self.set_busy(True)
        self.approval.setEnabled(False)
        self.status.setText("Закрываем сессию и удаляем учебную папку…")
        if self.stream:
            self.stream.requestInterruption()
        # Сначала завершаем текущие POST, затем закрываем созданную ими сессию.
        self.finish_close()

    def finish_close(self):
        if not self.closing or self.jobs:
            return
        if not self.client:
            self.close_done = True
            QTimer.singleShot(0, self.close)
            return
        if self.release_started:
            if self.stream:
                return
            self.close_done = True
            self.client.close()
            QTimer.singleShot(0, self.close)
            return
        self.release_started = True
        self.job(
            lambda: api.post(self.client, "/v0/close", "session.close", session_id=self.session),
            lambda _: None,
            self.close_failed,
        )

    @Slot(object)
    def close_failed(self, error):
        self.closing = False
        self.release_started = False
        self.set_busy(True)
        self.show_error(
            error_message(error) + " Закрытие сессии не подтверждено. Закройте окно ещё раз."
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("Agent Runtime")
    window = ChatWindow(args.url)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
