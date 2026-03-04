import json, os, qdarkstyle

from core.logger.advanced_logger import Logger
from ui.tabs.chat_tab import ChatTab
from ui.tabs.metrics_memory_tab import MetricsMemoryTab
from PySide6.QtWidgets import QMainWindow, QTabWidget, QVBoxLayout, QWidget


class MainWindow(QMainWindow):
    path = os.path.dirname(__file__)
    file_name = f"{os.path.splitext(os.path.basename(__file__))[0]}.json"
    CONFIG_FILE = os.path.join(path, file_name)

    # === Инициализация окна ===

    # Собирает вкладки приложения, применяет тему и восстанавливает состояние интерфейса из локального json-конфига.

    # Инициализирует внутреннее состояние объекта и связывает зависимости, которые будут использоваться остальными методами класса.

    def __init__(self, logger: Logger):
        super().__init__()
        self.logger = logger

        self.setWindowTitle("AI Challenge")
        self.setMinimumSize(1000, 720)
        self.setStyleSheet(qdarkstyle.load_stylesheet_pyside6())

        self.logger.info("Инициализация главного окна")
        self.init_ui()
        self.load_window_state()
        self.logger.success("Главное окно готово к работе")

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def init_ui(self):
        self.metrics_memory_tab = MetricsMemoryTab(logger=self.logger)
        self.chat_tab = ChatTab(logger=self.logger, metrics_memory_tab=self.metrics_memory_tab)

        self.tab_widget = QTabWidget()
        self.tab_widget.addTab(self.chat_tab, "Chat")
        self.tab_widget.addTab(self.metrics_memory_tab, "Metrics & Memory")
        self.tab_widget.setCurrentIndex(0)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        layout.setContentsMargins(0, 5, 0, 0)
        layout.addWidget(self.tab_widget)

    # === Сохранение и восстановление состояния ===

    # Сериализует геометрию/вкладки главного окна и при старте аккуратно восстанавливает их из сохранённого файла.

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def save_window_state(self):
        state = {
            "geometry": self.saveGeometry().toHex().data().decode(),
            "state": self.saveState().toHex().data().decode()
        }
        with open(self.CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

        self.logger.success("Состояние окна сохранено")

    # Загружает данные из источника, нормализует формат и возвращает объект, пригодный для дальнейшей обработки.

    def load_window_state(self):
        if not os.path.exists(self.CONFIG_FILE):
            return
        try:
            with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)

            self.restoreGeometry(bytes.fromhex(state["geometry"]))
            self.restoreState(bytes.fromhex(state["state"]))
            self.logger.debug("Состояние окна загружено")
        except Exception as e:
            self.logger.error(f"Ошибка загрузки состояния окна: {e}")

    # === Завершение приложения ===

    # Перед закрытием гарантированно сохраняет состояние окна, чтобы не терялась раскладка интерфейса.

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def closeEvent(self, event):
        self.logger.info("Закрытие приложения, сохранение состояния")
        self.save_window_state()
        super().closeEvent(event)
