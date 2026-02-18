import os, json, asyncio

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QSizePolicy, QProgressBar, QSplitter, QLabel,
    QLineEdit, QPushButton
)

from PySide6.QtCore import (Qt, QByteArray, QTimer, QEvent)
from PySide6.QtGui import QTextCursor, QFont

from ui.custom_objects.toggle_switch import ToggleSwitch
from ui.tabs.base_tab import BaseTab
from core.api.gptmodel import GPTModel
from extra.Global import (set_editbox_height)

class ChatTab(BaseTab):
    path = os.path.dirname(__file__)
    file_name = f"{os.path.splitext(os.path.basename(__file__))[0]}.json"
    CONFIG_FILE = os.path.join(path, file_name)

    def __init__(self, logger):
        super().__init__(logger)

        self.gpt = GPTModel()
        self.history = []

        # --- Служебные
        self.is_generating = False
        self.stop_requested = False
        self.current_task = None

        self.init_content()
        self.load_window_state()

        self.splitter_move_timer = QTimer(self)
        self.splitter_move_timer.setSingleShot(True)

        # ============ СЛУШАЕМ КРИКИ
        self.log_splitter.splitterMoved.connect(self.on_splitter_moved)
        self.vertical_splitter.splitterMoved.connect(self.on_splitter_moved)
        self.splitter_move_timer.timeout.connect(self.save_window_state)
        self.condition_toggle.toggled.connect(self.condition_toggle_changed)

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
        set_editbox_height(self.input_editbox, 8)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(8)

        # --- Поле для вывода ответа без условий
        self.output_editbox = QTextEdit()
        self.output_editbox.setFont(font)
        self.output_editbox.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.output_editbox.setReadOnly(True)
        set_editbox_height(self.output_editbox, 15)

        # --- Поле для вывода ответа с условиями
        self.output_editbox_with_condition = QTextEdit()
        self.output_editbox_with_condition.setFont(font)
        self.output_editbox_with_condition.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.output_editbox_with_condition.setReadOnly(True)
        set_editbox_height(self.output_editbox_with_condition, 15)

        # --- Тогл для включения условий
        self.condition_toggle = ToggleSwitch()
        self.condition_label = QLabel("Режим запуска с условиями:")
        self.condition_label.setFixedWidth(230)

        # --- Кнопки STOP под каждое окно
        self.stop_button_plain = QPushButton("STOP")
        self.stop_button_plain.setFixedWidth(150)
        self.stop_button_plain.setEnabled(False)
        self.stop_button_plain.clicked.connect(self.stop_generation_plain)

        self.clear_button_plain = QPushButton("CLEAR")
        self.clear_button_plain.setFixedWidth(150)
        self.clear_button_plain.setEnabled(False)
        self.clear_button_plain.clicked.connect(self.clear_output_editbox)
        self.output_editbox.textChanged.connect(self.set_enable_clear_button_plain)

        self.stop_button_condition = QPushButton("STOP")
        self.stop_button_condition.setFixedWidth(150)
        self.stop_button_condition.setEnabled(False)
        self.stop_button_condition.clicked.connect(self.stop_generation_condition)

        self.clear_button_condition = QPushButton("CLEAR")
        self.clear_button_condition.setFixedWidth(150)
        self.clear_button_condition.setEnabled(False)
        self.clear_button_condition.clicked.connect(self.clear_output_editbox_with_condition)
        self.output_editbox_with_condition.textChanged.connect(self.set_enable_clear_button_condition)

        # --- Поля условий (правая панель)
        self.format_label = QLabel("Формат ответа:")
        self.format_label.setFixedWidth(230)
        self.format_input = QLineEdit()
        self.format_input.setFixedWidth(350)
        self.format_input.setPlaceholderText("Например: Ровно 3 пункта, без вступления.")

        self.length_label = QLabel("Ограничение длины (слова/символы):")
        self.length_label.setFixedWidth(230)
        self.length_input = QLineEdit()
        self.length_input.setFixedWidth(350)
        self.length_input.setPlaceholderText("Например: Не более 60 слов.")

        self.stop_seq_label = QLabel("Stop sequence (строка завершения):")
        self.stop_seq_label.setFixedWidth(230)
        self.stop_seq_input = QLineEdit()
        self.stop_seq_input.setFixedWidth(350)
        self.stop_seq_input.setPlaceholderText("Например: ###END###")
        self.stop_seq_input.setText("###END###")

        self.max_tokens_label = QLabel("max_tokens (через API):")
        self.max_tokens_label.setFixedWidth(230)
        self.max_tokens_input = QLineEdit()
        self.max_tokens_input.setFixedWidth(350)
        self.max_tokens_input.setPlaceholderText("Например: 200")
        self.max_tokens_input.setText("200")

        # ============ РАССТАНОВКА ЭЛЕМЕНТОВ
        tab_layout = QVBoxLayout(self.top_widget)
        tab_layout.setContentsMargins(0, 0, 0, 0)

        input_container = QWidget()
        input_container.setFixedWidth(450)
        input_layout = QVBoxLayout(input_container)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(5)
        input_layout.addWidget(self.input_editbox)
        input_layout.addWidget(self.progress_bar, alignment=Qt.AlignTop)

        output_container = QWidget()
        output_container.setFixedWidth(900)
        output_layout = QHBoxLayout(output_container)
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.setSpacing(5)

        outbox_plain_buttons_container = QWidget()
        outbox_plain_layout = QHBoxLayout(outbox_plain_buttons_container)
        outbox_plain_layout.addWidget(self.stop_button_plain, alignment=Qt.AlignLeft)
        outbox_plain_layout.addWidget(self.clear_button_plain, alignment=Qt.AlignRight)
        
        plain_output_container = QWidget()
        plain_output_layout = QVBoxLayout(plain_output_container)
        plain_output_layout.setContentsMargins(0, 0, 0, 0)
        plain_output_layout.setSpacing(5)
        plain_output_layout.addWidget(self.output_editbox)
        plain_output_layout.addWidget(outbox_plain_buttons_container)

        outbox_condition_buttons_container = QWidget()
        outbox_condition_layout = QHBoxLayout(outbox_condition_buttons_container)
        outbox_condition_layout.addWidget(self.stop_button_condition, alignment=Qt.AlignLeft)
        outbox_condition_layout.addWidget(self.clear_button_condition, alignment=Qt.AlignRight)

        condition_output_container = QWidget()
        condition_output_layout = QVBoxLayout(condition_output_container)
        condition_output_layout.setContentsMargins(0, 0, 0, 0)
        condition_output_layout.setSpacing(5)
        condition_output_layout.addWidget(self.output_editbox_with_condition)
        condition_output_layout.addWidget(outbox_condition_buttons_container)

        output_layout.addWidget(plain_output_container)
        output_layout.addWidget(condition_output_container)

        union_container = QWidget()
        union_layout = QVBoxLayout(union_container)
        union_layout.addWidget(input_container, alignment=Qt.AlignHCenter)
        union_layout.addWidget(output_container, alignment=Qt.AlignHCenter)
        union_layout.addStretch()

        left_panel_container = QWidget()
        left_panel_layout = QHBoxLayout(left_panel_container)
        left_panel_layout.setContentsMargins(0, 0, 0, 0)
        left_panel_layout.addWidget(union_container, alignment=Qt.AlignLeft)

        condition_toggle_layout = QHBoxLayout()
        condition_toggle_layout.addWidget(self.condition_label, alignment=Qt.AlignLeft)
        condition_toggle_layout.addWidget(self.condition_toggle, alignment=Qt.AlignLeft)
        condition_toggle_layout.addStretch()

        format_layout = QHBoxLayout()
        format_layout.addWidget(self.format_label, alignment=Qt.AlignLeft)
        format_layout.addWidget(self.format_input, alignment=Qt.AlignLeft)

        length_layout = QHBoxLayout()
        length_layout.addWidget(self.length_label, alignment=Qt.AlignLeft)
        length_layout.addWidget(self.length_input, alignment=Qt.AlignLeft)

        stop_seq_layout = QHBoxLayout()
        stop_seq_layout.addWidget(self.stop_seq_label, alignment=Qt.AlignLeft)
        stop_seq_layout.addWidget(self.stop_seq_input, alignment=Qt.AlignLeft)
        
        max_tokens_layout = QHBoxLayout()
        max_tokens_layout.addWidget(self.max_tokens_label, alignment=Qt.AlignLeft)
        max_tokens_layout.addWidget(self.max_tokens_input, alignment=Qt.AlignLeft)

        right_panel_container = QWidget()
        right_panel_layout = QVBoxLayout(right_panel_container)
        right_panel_layout.setContentsMargins(0, 0, 0, 0)
        right_panel_layout.addLayout(condition_toggle_layout)
        right_panel_layout.addLayout(format_layout)
        right_panel_layout.addLayout(length_layout)
        right_panel_layout.addLayout(stop_seq_layout)
        right_panel_layout.addLayout(max_tokens_layout)

        right_panel_layout.addStretch()

        self.vertical_splitter = QSplitter(Qt.Horizontal)
        self.vertical_splitter.addWidget(left_panel_container)
        self.vertical_splitter.addWidget(right_panel_container)

        tab_layout.addWidget(self.vertical_splitter)

    def set_enable_clear_button_plain(self):
        state = True if self.output_editbox.toPlainText().strip() != "" else False
        self.clear_button_plain.setEnabled(state) 

    def set_enable_clear_button_condition(self):
        state = True if self.output_editbox_with_condition.toPlainText().strip() != "" else False
        self.clear_button_condition.setEnabled(state) 

    def clear_output_editbox(self):
        self.output_editbox.clear()
        self.set_enable_clear_button_plain()

    def clear_output_editbox_with_condition(self):
        self.output_editbox_with_condition.clear()
        self.set_enable_clear_button_condition()

    def condition_toggle_changed(self, state: bool):
        self.logger.info(f"Значение condition_toggle изменилось на {state}")
        self.history = []
        self.output_editbox_with_condition.clear() if state else self.output_editbox.clear()


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

        use_conditions = self.condition_toggle.isChecked()
        target_output = self.output_editbox_with_condition if use_conditions else self.output_editbox

        self.stop_button_plain.setEnabled(False)
        self.stop_button_condition.setEnabled(False)

        if use_conditions:
            self.stop_button_condition.setEnabled(True)
        else:
            self.stop_button_plain.setEnabled(True)

        target_output.append(f"Ты: {text} \n")
        target_output.append("GPT: ")

        self.set_loading(True)

        if use_conditions:
            fmt = self.format_input.text().strip()
            length_rule = self.length_input.text().strip()
            stop_seq = self.stop_seq_input.text().strip()

            instructions = []
            if fmt:
                instructions.append(f"Формат ответа: {fmt}")
            if length_rule:
                instructions.append(f"Ограничение длины: {length_rule}")
            if stop_seq:
                instructions.append(f"Условие завершения: в конце добавь строку {stop_seq} и после неё ничего не пиши.")

            controlled_text = text
            if instructions:
                controlled_text = text + "\n\n" + "\n".join(instructions)

            try:
                max_tokens = int(self.max_tokens_input.text().strip())
            except Exception:
                max_tokens = 200
                self.logger.warning("max_tokens задан неверно, использую 200.")
        else:
            controlled_text = text
            max_tokens = 800

        self.stop_requested = False
        self.is_generating = True
        self.current_task = asyncio.create_task(
            self.ask_and_stream_answer(controlled_text, target_output, use_conditions, max_tokens)
        )

    async def ask_and_stream_answer(self, user_text: str, target_output: QTextEdit, use_conditions: bool, max_tokens: int):
        self.logger.info("Отправка запроса в API")

        stop_seq = self.stop_seq_input.text().strip() if use_conditions else ""
        buffer_text = ""

        gen = None

        try:
            cursor = target_output.textCursor()
            cursor.movePosition(QTextCursor.End)
            target_output.setTextCursor(cursor)

            gen = self.gpt.stream_chat(
                user_text=user_text,
                system_text=None,
                history=None,
                max_tokens=max_tokens,
            )

            async for chunk in gen:
                if self.stop_requested:
                    break

                target_output.insertPlainText(chunk)
                target_output.moveCursor(QTextCursor.End)
                target_output.ensureCursorVisible()

                if use_conditions and stop_seq:
                    buffer_text += chunk
                    if stop_seq in buffer_text:
                        break

            target_output.append("")  # перенос строки после ответа

        except asyncio.CancelledError:
            try:
                target_output.append("\n[Остановлено пользователем]\n")
            except Exception:
                pass
            raise

        except Exception as e:
            self.logger.error_handler(e, context="ChatTab -> ask_and_stream_answer")
            target_output.append(f"\n[Ошибка] {e}\n")

        finally:
            # ВАЖНО: закрыть async generator всегда (и при stop-seq, и при STOP кнопке)
            if gen is not None:
                try:
                    await gen.aclose()
                except Exception:
                    pass

            self.is_generating = False
            self.current_task = None
            self.set_loading(False)

            self.stop_button_plain.setEnabled(False)
            self.stop_button_condition.setEnabled(False)

            self.logger.success("Ответ получен")

    def stop_generation_plain(self):
        self.stop_generation()

    def stop_generation_condition(self):
        self.stop_generation()

    def stop_generation(self):
        if not self.is_generating:
            return

        self.stop_requested = True

        if self.current_task is not None and not self.current_task.done():
            self.current_task.cancel()

        self.stop_button_plain.setEnabled(False)
        self.stop_button_condition.setEnabled(False)
        self.set_loading(False)

        self.logger.warning("Стрим остановлен пользователем.")

    def on_splitter_moved(self):
        self.splitter_move_timer.start(300)

    def save_window_state(self):
        try:
            state = {}
            if hasattr(self, "log_splitter"):
                state["log_splitter"] = self.log_splitter.saveState().toHex().data().decode()

            if hasattr(self, "vertical_splitter"):
                state["vertical_splitter"] = self.vertical_splitter.saveState().toHex().data().decode()

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
                    self.logger.error(f"Ошибка восстановления состояния log_splitter для вкладки \"Chat_tab\": {e}")
                    return
                
            if "vertical_splitter" in state:
                try:
                    splitter_state = QByteArray.fromHex(str(state["vertical_splitter"]).encode())
                    self.vertical_splitter.restoreState(splitter_state)
                except Exception as e:
                    self.logger.error(f"Ошибка восстановления состояния vertical_splitter вкладки \"Chat_tab\": {e}")
                    return

        except Exception as e:
            self.logger.error(f"Ошибка загрузки состояния окна для вкладки \"Chat_tab\": {e}")
            return

        self.logger.debug("Состояние вкладки загружено")
