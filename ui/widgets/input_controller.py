import asyncio
import extra.Global as Global


from core.agent.agent_client import AgentClient
from core.logger.advanced_logger import Logger

from ui.widgets.API_Controllers import APIControllers
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QSizePolicy, QProgressBar, QSplitter, QLabel,
    QLineEdit, QPushButton, QComboBox, QDoubleSpinBox, QListWidget, QListWidgetItem,
    QSpinBox
)
from PySide6.QtCore import (
    Qt,
    QEvent
)

class InputController(QWidget):
    def __init__(self, agent: AgentClient, logger: Logger):
        super().__init__()
        self.logger = logger
        self.agent = agent

        # --- ПЕРЕМЕННЫЕ ВИДЖЕТА
        self.is_generating = None
        
        self.init_content()

        # Обработка отправки через Enter
        

        # --- ПОДПИСКА НА СИГНАЛЫ (ЕСЛИ ЕСТЬ)
        
    def init_content(self):
        # --- UI объекты виджета
        
        self.API_controllers = APIControllers(logger=self.logger)
        self.API_controllers.setContentsMargins(0, 0, 0, 0) 

        self.textbox = QTextEdit()
        self.textbox.setMinimumWidth(400)
        self.textbox.setPlaceholderText("Ты можешь попробовать спросить, но не факт, что тебе кто-то ответит...")
        self.textbox.installEventFilter(self) 
        Global.set_editbox_height(self.textbox, 8)        

        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimumWidth(400)
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)

        # === РАССТАНОВКА ОБЪЕКТОВ ВИДЖЕТА
        widget_layout = QVBoxLayout(self)
        widget_layout.addWidget(self.API_controllers, alignment=Qt.AlignmentFlag.AlignHCenter)
        widget_layout.addWidget(self.textbox, alignment=Qt.AlignmentFlag.AlignHCenter)
        widget_layout.addWidget(self.progress_bar, alignment=Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)
    
    # ========= Enter отправляет, Shift+Enter перенос строки =========
    def eventFilter(self, obj, event):
        if obj is self.textbox and event.type() == QEvent.KeyPress:
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

    def on_send_message(self):
        if self.is_generating:
            self.logger.warning("Модель ещё отвечает — подожди.")
            return

        text = self.textbox.toPlainText().strip()
        if not text:
            self.logger.warning("Отсутствует текст для отправки!")
            return

        self.textbox.clear()

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

        # --- условия
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

        # --- параметры “сжатия”
        try:
            char_limit = int(self.char_limit_input.value())
        except Exception:
            char_limit = 12000

        try:
            keep_last_n = int(self.keep_last_n_input.value())
        except Exception:
            keep_last_n = 8

        summary_model = self.summary_model_selector.currentText().strip()
        summary_endpoint = self.summary_endpoint_selector.currentData()

        self.stop_requested = False
        self.is_generating = True
        self.current_task = asyncio.create_task(
            self.ask_and_stream_answer(
                controlled_text,
                target_output,
                use_conditions,
                max_tokens,
                char_limit,
                keep_last_n,
                summary_model,
                summary_endpoint,
            )
        )

    def set_loading(self, is_loading: bool):
        if is_loading:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)

    


