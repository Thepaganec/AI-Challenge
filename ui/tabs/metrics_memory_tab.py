from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTextEdit, QGroupBox, QHBoxLayout, QLabel, QComboBox, QLineEdit, QPushButton
)

from ui.tabs.base_tab import BaseTab
from core.logger.advanced_logger import Logger


class MetricsMemoryTab(BaseTab):
    def __init__(self, logger: Logger):
        super().__init__(logger)
        
        self.init_content()

    def init_content(self):
        layout = QVBoxLayout(self.top_widget)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(10)

        self.metrics_box = QTextEdit()
        self.metrics_box.setReadOnly(True)
        self.metrics_box.setMinimumHeight(120)

        metrics_group = QGroupBox("Metrics:")
        mg = QVBoxLayout(metrics_group)
        mg.setContentsMargins(8, 8, 8, 8)
        mg.addWidget(self.metrics_box)

        self.memory_layer_selector = QComboBox()
        self.memory_layer_selector.setFixedWidth(140)
        self.memory_layer_selector.addItem("short_term", "short_term")
        self.memory_layer_selector.addItem("working", "working")
        self.memory_layer_selector.addItem("long_term", "long_term")

        self.memory_key_input = QLineEdit()
        self.memory_key_input.setPlaceholderText("Ключ (опционально)")

        self.memory_value_input = QLineEdit()
        self.memory_value_input.setPlaceholderText("Значение для сохранения в память")

        self.save_memory_btn = QPushButton("Сохранить в память")

        self.memory_box = QTextEdit()
        self.memory_box.setReadOnly(True)
        self.memory_box.setMinimumHeight(140)

        memory_hint = QLabel(
            "Примечание: short_term заполняется автоматически из истории диалога; "
            "селектор слоя влияет только на кнопку 'Сохранить в память'."
        )
        memory_hint.setWordWrap(True)

        memory_group = QGroupBox("Memory layers:")
        ml = QVBoxLayout(memory_group)
        ml.setContentsMargins(8, 8, 8, 8)
        ml.setSpacing(6)

        mem_row1 = QWidget()
        mr1 = QHBoxLayout(mem_row1)
        mr1.setContentsMargins(0, 0, 0, 0)
        mr1.addWidget(QLabel("Слой:"))
        mr1.addStretch(1)
        mr1.addWidget(self.memory_layer_selector)

        ml.addWidget(mem_row1)
        ml.addWidget(self.memory_key_input)
        ml.addWidget(self.memory_value_input)
        ml.addWidget(self.save_memory_btn)
        ml.addWidget(memory_hint)
        ml.addWidget(self.memory_box)

        layout.addWidget(metrics_group)
        layout.addWidget(memory_group)
        layout.addStretch(1)

    def clear_panels(self):
        self.metrics_box.clear()
        self.memory_box.clear()
