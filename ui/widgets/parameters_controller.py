import extra.Global as Global

from core.logger.advanced_logger import Logger
from ui.custom_objects.toggle_switch import ToggleSwitch
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QSizePolicy, QProgressBar, QSplitter, QLabel,
    QLineEdit, QPushButton, QComboBox, QDoubleSpinBox, QListWidget, QListWidgetItem,
    QSpinBox
)
from PySide6.QtCore import (
    Qt,
    Signal
)

class ParametersController(QWidget):

    char_limit_changed = Signal(int)

    def __init__(self, logger: Logger):
        super().__init__()
        self.logger = logger
        # --- ПЕРЕМЕННЫЕ ВИДЖЕТА

        self.init_content()

        # --- ПОДПИСКА НА СИГНАЛЫ (ЕСЛИ ЕСТЬ)
        self.char_limit_input.valueChanged.connect(self.on_threshold_changed)
        self.toggle.toggled.connect(self.condition_toggle_changed)

    def init_content(self):
        # --- UI объекты виджета
        self.toggle = ToggleSwitch()
        self.toggle_label = QLabel("Режим запуска с условиями:")
        self.toggle_label.setFixedWidth(300)
        toggle_container = QWidget()
        toggle_layout = QHBoxLayout(toggle_container)
        toggle_layout.addWidget(self.toggle_label, alignment=Qt.AlignmentFlag.AlignLeft)
        toggle_layout.addWidget(self.toggle, alignment=Qt.AlignmentFlag.AlignLeft)
        toggle_layout.addStretch()

        self.char_limit_label = QLabel("Порог длины отправляемого сообщения (символы):")
        self.char_limit_label.setFixedWidth(300)
        self.char_limit_input = QSpinBox()
        self.char_limit_input.setRange(500, 200000)
        self.char_limit_input.setSingleStep(500)
        self.char_limit_input.setValue(12000)
        self.char_limit_input.setFixedWidth(140)
        char_limit_container = QWidget()
        char_limit_layout = QHBoxLayout(char_limit_container)
        char_limit_layout.addWidget(self.char_limit_label)
        char_limit_layout.addWidget(self.char_limit_input)
        char_limit_layout.addStretch()

        self.answer_format_label = QLabel("Формат ответа:")
        self.answer_format_label.setFixedWidth(300)
        self.answer_format_input = QLineEdit()
        self.answer_format_input.setFixedWidth(350)
        self.answer_format_input.setPlaceholderText("Например: Ровно 3 пункта, без вступления.")
        answer_format_container = QWidget()
        answer_format_layout = QHBoxLayout(answer_format_container)
        answer_format_layout.addWidget(self.answer_format_label)
        answer_format_layout.addWidget(self.answer_format_input)
        answer_format_layout.addStretch()

        self.answer_length_label = QLabel("Ограничение длины (слова/символы):")
        self.answer_length_label.setFixedWidth(300)
        self.answer_length_input = QLineEdit()
        self.answer_length_input.setFixedWidth(350)
        self.answer_length_input.setPlaceholderText("Например: Не более 60 слов.")
        answer_length_container = QWidget()
        answer_length_layout = QHBoxLayout(answer_length_container)
        answer_length_layout.addWidget(self.answer_length_label)
        answer_length_layout.addWidget(self.answer_length_input)
        answer_length_layout.addStretch()

        self.answer_stop_seq_label = QLabel("Строка завершения:")
        self.answer_stop_seq_label.setFixedWidth(300)
        self.answer_stop_seq_input = QLineEdit()
        self.answer_stop_seq_input.setFixedWidth(350)
        self.answer_stop_seq_input.setPlaceholderText("Например: ###END###")
        self.answer_stop_seq_input.setText("###END###")
        answer_stop_seq__container = QWidget()
        answer_stop_seq__layout = QHBoxLayout(answer_stop_seq__container)
        answer_stop_seq__layout.addWidget(self.answer_stop_seq_label)
        answer_stop_seq__layout.addWidget(self.answer_stop_seq_input)
        answer_stop_seq__layout.addStretch()

        self.max_tokens_label = QLabel("Кол-во токенов (через API):")
        self.max_tokens_label.setFixedWidth(300)
        self.max_tokens_input = QLineEdit()
        self.max_tokens_input.setFixedWidth(350)
        self.max_tokens_input.setPlaceholderText("Например: 200")
        self.max_tokens_input.setText("200")
        max_tokens_container = QWidget()
        max_tokens_layout = QHBoxLayout(max_tokens_container)
        max_tokens_layout.addWidget(self.max_tokens_label)
        max_tokens_layout.addWidget(self.max_tokens_input)
        max_tokens_layout.addStretch()

        # === РАССТАНОВКА ОБЪЕКТОВ ВИДЖЕТА
        widget_layout = QVBoxLayout(self)
        widget_layout.addWidget(char_limit_container)
        widget_layout.addWidget(toggle_container)
        widget_layout.addWidget(answer_format_container)
        widget_layout.addWidget(answer_length_container)
        widget_layout.addWidget(answer_stop_seq__container)
        widget_layout.addWidget(max_tokens_container)
        widget_layout.addStretch()

    def condition_toggle_changed(self, state: bool):
        self.logger.info(f"Значение condition_toggle изменилось на {state}")

    def on_threshold_changed(self):
        try:
            limit = int(self.char_limit_input.value())
        except Exception:
            limit = 0

        self.char_limit_changed.emit(limit)
    

