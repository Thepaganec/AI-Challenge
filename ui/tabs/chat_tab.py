import os, json, asyncio

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTextEdit, QSizePolicy, QProgressBar
)

from PySide6.QtCore import (Qt, QByteArray, QTimer, QEvent)
from PySide6.QtGui import QTextCursor, QFont

from ui.tabs.base_tab import BaseTab
from core.api.gptmodel import GPTModel


def set_editbox_height(editbox: QTextEdit, lines: int):
    fm = editbox.fontMetrics()
    line_height = fm.lineSpacing()

    extra = (
        int(editbox.document().documentMargin() * 2)
        + int(editbox.frameWidth() * 2)
        + 12
    )

    editbox.setFixedHeight(line_height * lines + extra)


class ChatTab(BaseTab):
    path = os.path.dirname(__file__)
    file_name = f"{os.path.splitext(os.path.basename(__file__))[0]}.json"
    CONFIG_FILE = os.path.join(path, file_name)

    def __init__(self, logger):
        super().__init__(logger)

        self.gpt = GPTModel()
        self.history = []
        self.is_generating = False

        self.init_content()
        self.load_window_state()

        # ============ СЛУШАЕМ КРИКИ
        self.log_splitter.splitterMoved.connect(self.on_splitter_moved)
        self.splitter_move_timer.timeout.connect(self.save_window_state)

        # Обработка отправки через Enter
        self.input_editbox.installEventFilter(self)

    def init_content(self):
        # ============ ОБЪЕКТЫ ВКЛАДКИ
        # --- Шрифт
        font = QFont()
        font.setPointSize(13)

        # --- Поле для ввода
        self.input_editbox = QTextEdit()
        self.input_editbox.setFont(font)
        self.input_editbox.setPlaceholderText(
            "Ты можешь попробовать спросить, но не факт, что тебе кто-то ответит..."
        )
        self.input_editbox.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        set_editbox_height(self.input_editbox, 5)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(8)

        # --- Поле для вывода
        self.output_editbox = QTextEdit()
        self.output_editbox.setFont(font)
        self.output_editbox.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.output_editbox.setReadOnly(True)
        set_editbox_height(self.output_editbox, 20)

        # ============ РАССТАНОВКА ЭЛЕМЕНТОВ
        tab_layout = QVBoxLayout(self.top_widget)
        tab_layout.setContentsMargins(0, 0, 0, 0)

        input_container = QWidget()
        input_container.setFixedWidth(700)
        input_layout = QVBoxLayout(input_container)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(6)

        input_layout.addWidget(self.input_editbox)
        input_layout.addWidget(self.progress_bar)
        input_layout.addWidget(self.output_editbox)

        tab_layout.addWidget(input_container, alignment=Qt.AlignHCenter)

        self.splitter_move_timer = QTimer(self)
        self.splitter_move_timer.setSingleShot(True)


    # ========= Enter отправляет, Shift+Enter перенос строки =========

    def eventFilter(self, obj, event):
        if obj is self.input_editbox and event.type() == QEvent.KeyPress:
            key = event.key()
            mods = event.modifiers()

            if key in (Qt.Key_Return, Qt.Key_Enter):
                # Shift+Enter — оставить стандартное поведение (новая строка)
                if mods & Qt.ShiftModifier:
                    return False

                # Enter — отправить
                self.on_send_message()
                return True

        return super().eventFilter(obj, event)

    def set_loading(self, is_loading: bool):
        if is_loading:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)


    def on_send_message(self):
        if self.is_generating:
            self.logger.warning("Модель ещё отвечает — подожди.")
            return

        text = self.input_editbox.toPlainText().strip()
        if not text:
            self.logger.warning("Отсутствует текст для отправки!")
            return

        self.input_editbox.clear()

        # Пишем в output “пользователь: …”
        self.output_editbox.append(f"Ты: {text} \n")
        self.output_editbox.append("GPT: ")

        # включаем индикатор сразу при старте
        self.set_loading(True)

        asyncio.create_task(self.ask_and_stream_answer(text))

    async def ask_and_stream_answer(self, user_text: str):
        self.logger.info("Отправка запроса в API")
        self.is_generating = True

        try:
            cursor = self.output_editbox.textCursor()
            cursor.movePosition(QTextCursor.End)
            self.output_editbox.setTextCursor(cursor)

            async for chunk in self.gpt.stream_chat(
                user_text=user_text,
                system_text=None,
                history=None,
                max_tokens=800,
            ):
                self.output_editbox.insertPlainText(chunk)
                self.output_editbox.moveCursor(QTextCursor.End)
                self.output_editbox.ensureCursorVisible()

            self.output_editbox.append("")  # перенос строки после ответа

        except Exception as e:
            self.logger.error_handler(e, context="ChatTab -> ask_and_stream_answer")
            self.output_editbox.append(f"\n[Ошибка] {e}\n")

        finally:
            self.is_generating = False
            self.set_loading(False)
            self.logger.success("Ответ получен")

    def on_splitter_moved(self):
        self.splitter_move_timer.start(300)

    def save_window_state(self):
        try:
            state = {}
            if hasattr(self, "log_splitter"):
                state["log_splitter"] = self.log_splitter.saveState().toHex().data().decode()

            with open(self.CONFIG_FILE, "w") as f:
                json.dump(state, f)

        except Exception as e:
            self.logger.error_handler(e, context="ChatTab -> save_window_state")
            return

    def load_window_state(self):
        if not os.path.exists(self.CONFIG_FILE):
            return
        try:
            with open(self.CONFIG_FILE, "r") as f:
                state = json.load(f)

            if "log_splitter" in state:
                try:
                    splitter_state = QByteArray.fromHex(str(state["log_splitter"]).encode())
                    self.log_splitter.restoreState(splitter_state)
                except Exception as e:
                    self.logger.error(f"Ошибка восстановления состояния сплиттера логов для вкладки \"Chat_tab\": {e}")
                    return

        except Exception as e:
            self.logger.error(f"Ошибка загрузки состояния окна для вкладки \"Chat_tab\": {e}")
            return

        self.logger.debug("Состояние вкладки загружено")
