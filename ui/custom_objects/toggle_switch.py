from PySide6.QtWidgets import QCheckBox
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QPainter, QColor

class ToggleSwitch(QCheckBox):

    # === Базовая настройка виджета ===

    # Инициализирует размеры и интерактивность кастомного переключателя на базе QCheckBox.

    # Инициализирует внутреннее состояние объекта и связывает зависимости, которые будут использоваться остальными методами класса.

    def __init__(self, parent=None, width=44, height=22):
        super().__init__(parent)
        self._w = int(width)
        self._h = int(height)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(self._w, self._h)
        self.setText("")

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def sizeHint(self):
        return QSize(self._w, self._h)

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def hitButton(self, pos):
        return self.rect().contains(pos)

    # === Кастомная отрисовка ===

    # Рисует фон и бегунок в зависимости от состояния checked, чтобы заменить стандартный стиль чекбокса на toggle.

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

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
