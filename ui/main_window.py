import json, os, qdarkstyle

from core.logger.advanced_logger import Logger 
from ui.tabs.chat_tab import ChatTab
from PySide6.QtWidgets import QMainWindow, QTabWidget, QVBoxLayout, QWidget

class MainWindow(QMainWindow):
    path = os.path.dirname(__file__)
    file_name = f"{os.path.splitext(os.path.basename(__file__))[0]}.json"
    CONFIG_FILE = os.path.join(path, file_name)

    def __init__(self, logger: Logger):
        super().__init__()
        self.logger = logger

        self.setWindowTitle("AI Challenge - Desktop App")
        self.setMinimumSize(800, 600)
        self.setStyleSheet(qdarkstyle.load_stylesheet_pyside6())

        self.logger.info("Инициализация главного окна")
        
        self.init_ui()
        self.load_window_state()

        self.logger.success("Главное окно готово к работе")

        # ============ ЛАЙК и ПОДПИСКА на КАНАЛ (на случай, если кто-то кричит)
        # Пока никто не кричит, но вдруг кто будет...

    def init_ui(self):
        # ============ ОБЪЕКТЫ
        self.chat_tab = ChatTab(logger=self.logger)

        # ============ РАССТАНОВКА ЭЛЕМЕНТОВ НА ЛАЙА-УТЕ, ПА-ПА-У-ТЭ-...У-ТЭ..ПА-ПА-У-ТЭ :)
        self.tab_widget = QTabWidget()
        self.tab_widget.addTab(self.chat_tab, "Вкладка с чат ботом")
        self.tab_widget.setCurrentIndex(0)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        central_widget_layout = QVBoxLayout(central_widget)
        central_widget_layout.setContentsMargins(0, 5, 0, 0)
        central_widget_layout.addWidget(self.tab_widget)
        
    def save_window_state(self):
        state = {
            "geometry": self.saveGeometry().toHex().data().decode(),
            "state": self.saveState().toHex().data().decode()
        }
        with open(self.CONFIG_FILE, "w") as f:
            json.dump(state, f)

        self.logger.success("Состояние окна сохранено")

    def load_window_state(self):
        if not os.path.exists(self.CONFIG_FILE):
            return
        try:
            with open(self.CONFIG_FILE, "r") as f:
                state = json.load(f)

            self.restoreGeometry(bytes.fromhex(state["geometry"]))
            self.restoreState(bytes.fromhex(state["state"]))
        except Exception as e:
            self.logger.error(f"Ошибка загрузки состояния окна: {e}")
            return

        self.logger.debug(f"Состояние окна загружено")

    def closeEvent(self, event):
        self.logger.info("Закрытие приложения, сохранение состояния")
        self.save_window_state()
        super().closeEvent(event)
