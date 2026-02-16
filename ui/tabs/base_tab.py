import os

from core.logger.advanced_logger import Logger
from PySide6.QtWidgets import (
    QWidget, 
    QSplitter, 
    QVBoxLayout, 
    QTextEdit, 
    QCheckBox, 
    QHBoxLayout
    )
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor

class BaseTab(QWidget):
    path = os.path.dirname(__file__)
    file_name = f"{os.path.splitext(os.path.basename(__file__))[0]}.json"
    CONFIG_FILE = os.path.join(path, file_name)

    def __init__(self, logger: Logger):
        super().__init__()
        self.logger = logger

        self.init_ui()

        # ============= СЛУШАЕМ КРИКИ
        self.logger.log_signal.connect(self.append_log_message)

    def init_ui(self):
        self.top_widget = QWidget()
        self.log_widget = QTextEdit()
        self.log_widget.setReadOnly(True)
        self.log_widget.setMinimumHeight(100)

        self.auto_scroll_checkbox = QCheckBox("Автоскролл")
        self.auto_scroll_checkbox.setChecked(False)

        log_control_layout = QHBoxLayout()
        log_control_layout.setContentsMargins(5, 2, 5, 2)
        log_control_layout.addWidget(self.auto_scroll_checkbox)
        log_control_layout.addStretch()

        log_container = QWidget()
        log_layout = QVBoxLayout(log_container)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.addLayout(log_control_layout)
        log_layout.addWidget(self.log_widget)

        self.log_splitter = QSplitter(Qt.Vertical)
        self.log_splitter.addWidget(self.top_widget)
        self.log_splitter.addWidget(log_container)

        tab_layout = QVBoxLayout(self)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.addWidget(self.log_splitter)

    def append_log_message(self, message, color="white"):
        self.log_widget.setTextColor(QColor(color))
        self.log_widget.append(message.strip())

        if hasattr(self, "auto_scroll_checkbox") and self.auto_scroll_checkbox.isChecked():
            QTimer.singleShot(0, self.scroll_log_to_bottom)

    def scroll_log_to_bottom(self):
        scrollbar = self.log_widget.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

