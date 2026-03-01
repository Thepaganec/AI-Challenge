import extra.Global as Global


from ui.widgets.API_Controllers import APIControllers
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QSizePolicy, QProgressBar, QSplitter, QLabel,
    QLineEdit, QPushButton, QComboBox, QDoubleSpinBox, QListWidget, QListWidgetItem,
    QSpinBox
)
from PySide6.QtCore import (
    Qt
)

class SummarizationController(QWidget):
    def __init__(self):
        super().__init__()

        # --- ПЕРЕМЕННЫЕ ВИДЖЕТА

        self.init_content()

        # --- ПОДПИСКА НА СИГНАЛЫ (ЕСЛИ ЕСТЬ)

    def init_content(self):
        # --- UI объекты виджета
        self.API_controllers = APIControllers()
        self.API_controllers.setContentsMargins(0, 0, 0, 0) 

        self.label = QLabel("Суммаризация, если есть:")
        self.textbox = QTextEdit()
        self.textbox.setReadOnly(True)
        self.textbox.setPlaceholderText("Здесь будет появляться суммаризация истории...")

        # === РАССТАНОВКА ОБЪЕКТОВ ВИДЖЕТА
        widget_layout = QVBoxLayout(self)
        widget_layout.addWidget(self.API_controllers, alignment=Qt.AlignmentFlag.AlignTop)
        widget_layout.addWidget(self.label, alignment=Qt.AlignmentFlag.AlignLeft)
        widget_layout.addWidget(self.textbox)
    

