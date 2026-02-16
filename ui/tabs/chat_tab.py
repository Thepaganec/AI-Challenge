import os, json

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QSplitter, QLabel, QMessageBox, QTextEdit, QSizePolicy
)
from PySide6.QtCore import (Qt, Signal, QByteArray, QTimer)

from ui.tabs.base_tab import BaseTab

def set_textbox_height(textbox: QTextEdit, lines: int=5):
    fm = textbox.fontMetrics()
    line_height = fm.lineSpacing()

    extra = (
        int(textbox.document().documentMargin() * 2)
        + int(textbox.frameWidth() * 2)
        + 12
    )

    height = line_height * lines + extra
    textbox.setFixedHeight(height)

class ChatTab(BaseTab):
    path = os.path.dirname(__file__)
    file_name = f"{os.path.splitext(os.path.basename(__file__))[0]}.json"
    CONFIG_FILE = os.path.join(path, file_name)

    def __init__(self, logger):
        super().__init__(logger)

        self.init_content()
        self.load_window_state()

        # ============ СЛУШАЕМ КРИКИ
        self.log_splitter.splitterMoved.connect(self.on_splitter_moved)
        self.splitter_move_timer.timeout.connect(self.save_window_state)

    def init_content(self):
        # ============ ОБЪЕКТЫ ВКЛАДКИ
        # --- Поле для ввода
        self.input_editbox = QTextEdit()
        self.input_editbox.setPlaceholderText(
            f"Ты можешь попробовать спросить, но не факт, что тебе кто-то ответит..."
        )
        self.input_editbox.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        set_textbox_height(self.input_editbox, 5)

        # --- Полей для вывода 
        self.output_editbox = QTextEdit()
        self.output_editbox.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.output_editbox.setReadOnly(True)
        set_textbox_height(self.output_editbox, 20)
        
        # ============ РАССТАНОВКА ЭЛЕМЕНТОВ
        tab_layout = QVBoxLayout(self.top_widget)
        tab_layout.setContentsMargins(0, 0, 0, 0)

        input_container = QWidget()
        input_container.setFixedWidth(700)

        input_layout = QVBoxLayout(input_container)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(0)
        input_layout.addWidget(self.input_editbox)
        input_layout.addWidget(self.output_editbox)

        tab_layout.addWidget(input_container, alignment=Qt.AlignHCenter)

        self.splitter_move_timer = QTimer(self)
        self.splitter_move_timer.setSingleShot(True)
        
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

        self.logger.debug(f"Состояние вкладки загружено")

