from PySide6.QtWidgets import QCheckBox
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QPainter, QColor

class ToggleSwitch(QCheckBox):
    def __init__(self, parent=None, width=44, height=22):
        super().__init__(parent)
        self._w = int(width)
        self._h = int(height)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(self._w, self._h)
        self.setText("")

    def sizeHint(self):
        return QSize(self._w, self._h)

    def hitButton(self, pos):
        return self.rect().contains(pos)
    
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        radius = self._h / 2
        margin = 2
        knob_d = self._h - margin * 2

        # фон
        bg = QColor("#00c853") if self.isChecked() else QColor("#777777")
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(0, 0, self._w, self._h, radius, radius)

        # “кнопка”
        x = self._w - knob_d - margin if self.isChecked() else margin
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(int(x), margin, int(knob_d), int(knob_d))
        p.end()
