"""Учебный чат: HTTP-команды и SSE, без доступа к папкам Mac."""

import argparse
import os
import sys
from uuid import uuid4

import httpx2
from PySide6.QtCore import Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import (
    QFont,
    QFontDatabase,
    QKeySequence,
    QShortcut,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from client import api
from client.appearance import DiffHighlight, apply_theme


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


def label(text, style):
    widget = QLabel(text)
    widget.setObjectName(style)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    return widget


def panel(name="panel"):
    widget = QFrame()
    widget.setObjectName(name)
    return widget


def text_view(name, placeholder="", fixed=False):
    view = QPlainTextEdit()
    view.setObjectName(name)
    view.setPlaceholderText(placeholder)
    view.setReadOnly(True)
    view.document().setDocumentMargin(10)
    if fixed:
        view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    return view


def labeled_field(title, field):
    layout = QVBoxLayout()
    layout.setSpacing(4)
    caption = label(title, "fieldLabel")
    caption.setBuddy(field)
    layout.addWidget(caption)
    layout.addWidget(field)
    return layout


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
        self.setWindowTitle("Agent Runtime")
        self.resize(1120, 780)
        self.setMinimumSize(720, 640)
        root = QWidget()
        root.setObjectName("workspace")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(22, 18, 22, 15)
        layout.setSpacing(12)

        header = QHBoxLayout()
        header.setSpacing(12)
        brand = label("AR", "brand")
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand.setFixedSize(44, 44)
        heading = QVBoxLayout()
        heading.setSpacing(2)
        heading.addWidget(label("Agent Runtime", "title"))
        heading.addWidget(label("Рабочая среда агента", "caption"))
        header.addWidget(brand)
        header.addLayout(heading, 1)
        header.addWidget(label("Учебная сессия", "badge"))
        layout.addLayout(header)
        layout.addWidget(
            self.connection_panel(url, token or os.getenv("CLIENT_TOKEN", "local-dev-token"))
        )

        split = QSplitter()
        split.setChildrenCollapsible(False)
        split.setHandleWidth(12)
        split.addWidget(self.conversation_panel())
        split.addWidget(self.session_panel())
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([660, 400])
        layout.addWidget(split, 1)

        self.status = label("Нет подключения", "status")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        hints = QApplication.styleHints()
        hints.colorSchemeChanged.connect(self.set_theme)
        self.set_theme(hints.colorScheme())
        self.set_busy(False)

    def connection_panel(self, url, token):
        widget = panel("connection")
        row = QHBoxLayout(widget)
        row.setContentsMargins(13, 10, 13, 12)
        row.setSpacing(12)
        self.url = QLineEdit(url)
        self.url.setAccessibleName("Адрес сервера")
        self.token = QLineEdit(token)
        self.token.setEchoMode(QLineEdit.EchoMode.Password)
        self.token.setAccessibleName("Токен клиента")
        self.connect_button = QPushButton("Подключить чат")
        self.connect_button.setProperty("primary", True)
        self.connect_button.clicked.connect(self.connect_chat)
        row.addLayout(labeled_field("Сервер", self.url), 3)
        row.addLayout(labeled_field("Токен", self.token), 2)
        row.addWidget(self.connect_button, 0, Qt.AlignmentFlag.AlignBottom)
        return widget

    def conversation_panel(self):
        widget = panel()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(15, 12, 15, 12)
        layout.setSpacing(8)
        layout.addWidget(label("Чат", "section"))
        self.chat = QTextEdit()
        self.chat.setObjectName("chat")
        self.chat.setReadOnly(True)
        self.chat.setAcceptRichText(False)
        self.chat.setPlaceholderText("Подключите чат, чтобы отправить первую задачу.")
        self.chat.document().setDocumentMargin(10)
        self.chat.setMinimumHeight(72)
        layout.addWidget(self.chat, 1)
        layout.addWidget(self.approval_panel())

        self.message = QPlainTextEdit()
        self.message.setObjectName("composer")
        self.message.setPlaceholderText("Например: прочитай note.txt и замени original на changed")
        self.message.setFixedHeight(73)
        self.message.setAccessibleName("Сообщение агенту")
        self.message.setTabChangesFocus(True)
        self.message.document().setDocumentMargin(10)
        layout.addWidget(self.message)
        actions = QHBoxLayout()
        shortcut = QKeySequence("Ctrl+Return")
        shortcut_hint = shortcut.toString(QKeySequence.SequenceFormat.NativeText)
        actions.addWidget(label(f"Отправка · {shortcut_hint}", "caption"))
        actions.addStretch()
        self.send_button, self.cancel_button = QPushButton("Отправить"), QPushButton("Отмена")
        self.send_button.setProperty("primary", True)
        self.send_button.clicked.connect(self.send)
        self.cancel_button.clicked.connect(self.cancel)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.send_button)
        layout.addLayout(actions)
        QShortcut(shortcut, self, activated=self.send)
        return widget

    def approval_panel(self):
        self.approval = panel("approval")
        layout = QVBoxLayout(self.approval)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        layout.addWidget(label("Нужно разрешение", "approvalTitle"))
        self.operation = QLabel()
        self.operation.setWordWrap(True)
        self.operation.setTextFormat(Qt.TextFormat.PlainText)
        self.preview = text_view("preview", fixed=True)
        self.preview.setMinimumHeight(60)
        self.preview.setMaximumHeight(108)
        self.diff = DiffHighlight(self.preview.document())
        layout.addWidget(self.operation)
        layout.addWidget(self.preview)
        decisions = QHBoxLayout()
        self.allow, self.deny = QPushButton("Разрешить"), QPushButton("Отклонить")
        self.allow.setProperty("primary", True)
        self.allow.clicked.connect(lambda: self.decide(True))
        self.deny.clicked.connect(lambda: self.decide(False))
        decisions.addStretch()
        decisions.addWidget(self.deny)
        decisions.addWidget(self.allow)
        layout.addLayout(decisions)
        self.approval.hide()
        return self.approval

    def session_panel(self):
        widget = panel()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(13, 12, 13, 12)
        layout.setSpacing(8)
        layout.addWidget(label("Сессия", "section"))
        hint = label("Учебные файлы удаляются при закрытии чата.", "caption")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.activity = text_view("activity", "Здесь появятся действия агента.")
        self.activity.setMaximumBlockCount(2000)
        self.file_content = text_view("fileContent", "Выберите файл.", fixed=True)
        self.file_list = QListWidget()
        self.file_list.setAccessibleName("Файлы сессии")
        self.file_list.currentTextChanged.connect(self.show_file)
        files = QSplitter(Qt.Orientation.Vertical)
        files.setChildrenCollapsible(False)
        files.addWidget(self.file_list)
        files.addWidget(self.file_content)
        files.setSizes([96, 360])
        tabs = QTabWidget()
        tabs.addTab(self.activity, "Действия")
        tabs.addTab(files, "Файлы")
        layout.addWidget(tabs, 1)
        return widget

    @Slot(Qt.ColorScheme)
    def set_theme(self, scheme):
        self.diff.colors = apply_theme(
            self,
            scheme,
            (self.url, self.token, self.chat, self.activity, self.file_content, self.message),
        )
        self.diff.rehighlight()

    def append_message(self, author, message):
        cursor = QTextCursor(self.chat.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        header = QTextBlockFormat()
        header.setTopMargin(12)
        header.setBottomMargin(5)
        if cursor.position():
            cursor.insertBlock(header)
        else:
            cursor.setBlockFormat(header)
        bold = QTextCharFormat()
        bold.setFontWeight(QFont.Weight.Bold)
        cursor.insertText(author, bold)
        cursor.insertBlock(QTextBlockFormat(), QTextCharFormat())
        cursor.insertText(message, QTextCharFormat())
        self.chat.verticalScrollBar().setValue(self.chat.verticalScrollBar().maximum())

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
        self.append_message("Вы", message)
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
            self.append_message("Агент", data["text"])
        elif kind == "approval_required":
            self.pending = data["operation_id"]
            operation = data["operation"]
            self.operation.setText(
                "Запись файла: " + operation["path"]
                if operation["kind"] == "WriteFile"
                else "Выполнение команды в сандбоксе"
            )
            self.diff.enabled = "diff" in data
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
