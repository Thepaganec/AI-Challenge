import extra.Global as Global

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QSizePolicy, QProgressBar, QSplitter, QLabel,
    QLineEdit, QPushButton, QComboBox, QDoubleSpinBox, QListWidget, QListWidgetItem,
    QSpinBox
)
from PySide6.QtCore import (
    Qt,
    Signal
)

class OutputController(QWidget):

    current_message_length_changed = Signal()
    length_threshold_changed = Signal()

    def __init__(self):
        super().__init__()

        # --- ПЕРЕМЕННЫЕ ВИДЖЕТА
        self.length_threshold = 0
        self.current_message_length = 0

        self.init_content()
        self.set_label()

        # --- ПОДПИСКА НА СИГНАЛЫ (ЕСЛИ ЕСТЬ)
        self.length_threshold_changed.connect(self.set_label)
        self.current_message_length_changed.connect(self.set_label)
        #self.stop_button.clicked.connect(self.stop_generation)
        #self.clear_button.clicked.connect(self.clear_textbox)
        #self.textbox.textChanged.connect(self.set_enable_clear_button)

    def init_content(self):
        # --- UI объекты виджета
        self.label = QLabel("")
        self.label.setFont(Global.get_default_font())

        self.textbox = QTextEdit()
        self.textbox.setMinimumWidth(700)
        self.textbox.setFont(Global.get_default_font())
        self.textbox.setReadOnly(True)
        Global.set_editbox_height(self.textbox, 12)

        self.stop_button = QPushButton("STOP")
        self.stop_button.setFixedWidth(150)
        self.stop_button.setEnabled(False)

        self.clear_button = QPushButton("CLEAR")
        self.clear_button.setFixedWidth(150)
        self.clear_button.setEnabled(False)

        buttons_container = QWidget()
        buttons_layout = QHBoxLayout(buttons_container)
        buttons_layout.addWidget(self.stop_button)
        buttons_layout.addWidget(self.clear_button)

        # === РАССТАНОВКА ОБЪЕКТОВ ВИДЖЕТА
        widget_layout = QVBoxLayout(self)
        widget_layout.addWidget(self.label, alignment=Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignBottom)
        widget_layout.addWidget(self.textbox, alignment=Qt.AlignmentFlag.AlignHCenter)
        widget_layout.addWidget(buttons_container, alignment=Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop)

    def set_current_message_length(self, msg_length: int):
        self.current_message_length = msg_length
        self.current_message_length_changed.emit()

    def set_length_threshold(self, threshold_length: int):
        self.length_threshold = threshold_length
        self.length_threshold_changed.emit()

    def set_label(self):
        self.label.setText(f"Длина истории: {self.current_message_length} / {self.length_threshold}")

         

