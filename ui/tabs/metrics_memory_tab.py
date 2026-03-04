from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QTextEdit, QGroupBox, QHBoxLayout, QLabel, QComboBox, QLineEdit, QPushButton, QSplitter
)
from PySide6.QtCore import Qt

from ui.tabs.base_tab import BaseTab
from ui.custom_objects.toggle_switch import ToggleSwitch
from core.logger.advanced_logger import Logger


class MetricsMemoryTab(BaseTab):

    # === Инициализация вкладки ===

    # Создаёт базовый контейнер вкладки метрик и памяти поверх общего BaseTab.

    # Инициализирует внутреннее состояние объекта и связывает зависимости, которые будут использоваться остальными методами класса.

    def __init__(self, logger: Logger):
        super().__init__(logger)
        
        self.init_content()

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

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

        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(10)
        left_layout.addWidget(metrics_group)
        left_layout.addWidget(memory_group)
        left_layout.addStretch(1)

        self.profile_selector = QComboBox()
        self.profile_selector.setEditable(True)
        self.profile_selector.setInsertPolicy(QComboBox.NoInsert)
        self.profile_selector.setMinimumWidth(220)
        self.profile_selector.setToolTip("Выберите существующий профиль или введите имя нового.")

        self.profile_description_input = QTextEdit()
        self.profile_description_input.setMinimumHeight(180)
        self.profile_description_input.setPlaceholderText(
            "Опишите профиль пользователя в свободной форме.\n"
            "Можно указать: стиль ответа, формат ответа, ограничения, предпочтения."
        )

        self.profile_use_toggle = ToggleSwitch()
        self.profile_use_toggle.setEnabled(False)

        self.save_profile_btn = QPushButton("Сохранить профиль")
        self.delete_profile_btn = QPushButton("Удалить профиль")

        profile_group = QGroupBox("Профиль пользователя:")
        pg = QVBoxLayout(profile_group)
        pg.setContentsMargins(8, 8, 8, 8)
        pg.setSpacing(8)

        profile_row1 = QWidget()
        pr1 = QHBoxLayout(profile_row1)
        pr1.setContentsMargins(0, 0, 0, 0)
        pr1.setSpacing(8)
        pr1.addWidget(QLabel("Имя профиля:"))
        pr1.addStretch(1)
        pr1.addWidget(self.profile_selector)

        profile_row2 = QWidget()
        pr2 = QHBoxLayout(profile_row2)
        pr2.setContentsMargins(0, 0, 0, 0)
        pr2.setSpacing(8)
        pr2.addWidget(QLabel("Использовать профиль:"))
        pr2.addStretch(1)
        pr2.addWidget(self.profile_use_toggle, alignment=Qt.AlignRight)

        profile_row3 = QWidget()
        pr3 = QHBoxLayout(profile_row3)
        pr3.setContentsMargins(0, 0, 0, 0)
        pr3.setSpacing(8)
        pr3.addWidget(self.save_profile_btn)
        pr3.addWidget(self.delete_profile_btn)
        pr3.addStretch(1)

        profile_hint = QLabel(
            "Подсказка: в одном описании профиля можно указывать стиль ответа, "
            "формат ответа и ограничения."
        )
        profile_hint.setWordWrap(True)

        pg.addWidget(profile_row1)
        pg.addWidget(profile_row2)
        pg.addWidget(self.profile_description_input)
        pg.addWidget(profile_hint)
        pg.addWidget(profile_row3)
        pg.addStretch(1)

        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addWidget(profile_group)

        self.metrics_splitter = QSplitter(Qt.Horizontal)
        self.metrics_splitter.addWidget(left_widget)
        self.metrics_splitter.addWidget(right_widget)
        self.metrics_splitter.setStretchFactor(0, 3)
        self.metrics_splitter.setStretchFactor(1, 2)

        layout.addWidget(self.metrics_splitter)

    # === Очистка панелей ===

    # Сбрасывает отображаемые метрики и снимок memory layers без изменения данных на сервере.

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def clear_panels(self):
        self.metrics_box.clear()
        self.memory_box.clear()
