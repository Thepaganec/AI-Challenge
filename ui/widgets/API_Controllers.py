import extra.Global as Global
from core.logger.advanced_logger import Logger
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QSizePolicy, QProgressBar, QSplitter, QLabel,
    QLineEdit, QPushButton, QComboBox, QDoubleSpinBox, QListWidget, QListWidgetItem,
    QSpinBox
)
from PySide6.QtCore import (
    Qt
)

class APIControllers(QWidget):
    def __init__(self, logger: Logger):
        super().__init__()
        self.logger = logger

        # --- ПЕРЕМЕННЫЕ ВИДЖЕТА
        self.model_list = [
            "gpt-3.5-turbo", 
            "gpt-4o-mini", 
            "gpt-4o", 
            "gpt-5.2-chat-latest"]
        self.endpoint_list = [
            ("Chat Completions", "chat"),
            ("Responses", "responses")]

        self.init_content()
        self.on_model_changed(self.model_selector.currentText())

        # --- ПОДПИСКА НА СИГНАЛЫ (ЕСЛИ ЕСТЬ)
        self.model_selector.currentTextChanged.connect(self.on_model_changed)

    def init_content(self):
        # --- UI объекты виджета
        self.model_label = QLabel("Модель:")
        self.model_selector = QComboBox()
        self.model_selector.setFixedWidth(200)
        self.model_selector.addItems(self.model_list)

        self.endpoint_label = QLabel("Эндпоинт:")
        self.endpoint_selector = QComboBox()
        self.endpoint_selector.setFixedWidth(200)
        for name, internal_id in self.endpoint_list:
            self.endpoint_selector.addItem(name, internal_id)

        self.temperature_label = QLabel("Температура:")
        self.temperature_input = QDoubleSpinBox()
        self.temperature_input.setFixedWidth(100)
        self.temperature_input.setDecimals(1)
        self.temperature_input.setSingleStep(0.1)
        self.temperature_input.setRange(0.0, 2.0)
        self.temperature_input.setValue(1.0)

        controllers_container = QWidget()
        controllers_layout = QHBoxLayout(controllers_container)
        controllers_layout.addWidget(self.model_selector, alignment=Qt.AlignmentFlag.AlignHCenter)
        controllers_layout.addWidget(self.endpoint_selector, alignment=Qt.AlignmentFlag.AlignHCenter)
        controllers_layout.addWidget(self.temperature_label, alignment=Qt.AlignmentFlag.AlignHCenter)
        controllers_layout.addWidget(self.temperature_input, alignment=Qt.AlignmentFlag.AlignHCenter)

        # === РАССТАНОВКА ОБЪЕКТОВ ВИДЖЕТА
        widget_layout = QVBoxLayout(self)
        widget_layout.addWidget(controllers_container)

    def on_model_changed(self, model_text: str):
        model_text = (model_text or "").strip()

        # Для openai/gpt-5.2-chat-latest ProxyAPI запрещает temperature != 1
        is_gpt52_locked = (model_text == "gpt-5.2-chat-latest")

        self.temperature_input.setEnabled(not is_gpt52_locked)

        if is_gpt52_locked:
            self.temperature_input.setValue(1.0)
            self.logger.warning("Для gpt-5.2-chat-latest temperature заблокирована ProxyAPI. Установлено 1.0.")
        else:
            self.logger.info(f"Выбрана модель {model_text}. temperature доступна.")

            
    

