import extra.Global as Global

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QSizePolicy, QProgressBar, QSplitter, QLabel,
    QLineEdit, QPushButton, QComboBox, QDoubleSpinBox, QListWidget, QListWidgetItem,
    QSpinBox
)
from PySide6.QtCore import (
    Qt
)

class MetricsController(QWidget):
    def __init__(self):
        super().__init__()

        # --- ПЕРЕМЕННЫЕ ВИДЖЕТА

        self.init_content()

        # --- ПОДПИСКА НА СИГНАЛЫ (ЕСЛИ ЕСТЬ)

    def init_content(self):
        # --- UI объекты виджета
        self.metrics_label = QLabel("Результаты замеров:")
        self.textbox = QTextEdit()
        self.textbox.setMinimumHeight(150)
        self.textbox.setReadOnly(True)
        self.textbox.setPlaceholderText(
            "Здесь будет появляться результат каждой попытки:\n"
            "TTFT / Total time / Tokens / Cost / Model / Endpoint / Temperature..."
        )
        # === РАССТАНОВКА ОБЪЕКТОВ ВИДЖЕТА
        widget_layout = QVBoxLayout(self)
        widget_layout.addWidget(self.metrics_label, alignment=Qt.AlignmentFlag.AlignLeft)
        widget_layout.addWidget(self.textbox)
    

