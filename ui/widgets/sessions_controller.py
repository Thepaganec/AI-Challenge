import asyncio, uuid
import extra.Global as Global
from core.agent.agent_client import AgentClient
from core.logger.advanced_logger import Logger

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QSizePolicy, QProgressBar, QSplitter, QLabel,
    QLineEdit, QPushButton, QComboBox, QDoubleSpinBox, QListWidget, QListWidgetItem,
    QSpinBox
)
from PySide6.QtCore import (
    Qt
)

class SessionsController(QWidget):
    def __init__(self, agent: AgentClient, logger: Logger):
        super().__init__()
        # --- ПЕРЕМЕННЫЕ ВИДЖЕТА
        self.agent = agent
        self.logger = logger
        self.selected_session_id = None

        self.init_content()

        # --- ПОДПИСКА НА СИГНАЛЫ (ЕСЛИ ЕСТЬ)
        #self.listbox.itemClicked.connect(self.on_session_clicked)
        #self.new_session_button.clicked.connect(self.on_new_session_clicked)
        #self.clear_session_button.clicked.connect(self.on_clear_session_clicked)
        self.agent.sessions_list_updated.connect(
            lambda: asyncio.get_event_loop().create_task(self.load_session_list()))

    def init_content(self):
        # --- UI объекты виджета
        self.listbox = QListWidget(self)
        self.listbox.setFixedHeight(100)

        self.new_session_button = QPushButton("Новая сессия")
        self.clear_session_button = QPushButton("Очистить сессию")

        sessions_buttons_container = QWidget()
        sessions_buttons_layout = QHBoxLayout(sessions_buttons_container)
        sessions_buttons_layout.addWidget(self.new_session_button)
        sessions_buttons_layout.addWidget(self.clear_session_button)

        # === РАССТАНОВКА ОБЪЕКТОВ ВИДЖЕТА
        widget_layout = QVBoxLayout(self)
        widget_layout.addWidget(self.listbox)
        widget_layout.addWidget(sessions_buttons_container)

    async def load_session_list(self):
        if not self.agent.is_connected:
            self.logger.warning("Агент не подключен -> список сессий не может быть загружен")
            return
        
        self.listbox.clear()

        if self.agent.sessions_list and not []:
            try:
                for s in self.agent.sessions_list:
                    sid = (s.get("session_id") or "").strip()
                    title = (s.get("title") or "").strip()
                    if not sid:
                        continue

                    label = f"{sid} — {title or 'Без темы'}"
                    item = QListWidgetItem(label)
                    item.setData(Qt.UserRole, sid)
                    self.listbox.addItem(item)  
            except Exception as e:
                self.logger.warning(f"Не удалось получить список сессий: {e}")
                return
        else:
            self.selected_session_id = None

              

"""

    def on_session_clicked(self, item: QListWidgetItem):
        sid = item.data(Qt.UserRole)
        if not sid:
            return

        self.current_session_id = str(sid)
        asyncio.get_event_loop().create_task(self.load_session_to_ui(self.current_session_id))
    
    def on_new_session_clicked(self):
        if self.is_generating:
            self.logger.warning("Нельзя сменить сессию во время генерации.")
            return

        self.current_session_id = str(uuid.uuid4())

        try:
            self.output_editbox.clear()
            self.output_editbox_with_condition.clear()
        except Exception:
            pass

        self.logger.success(f"Создана новая сессия: {self.current_session_id}")

        if self.is_agent_connected:
            asyncio.get_event_loop().create_task(self.refresh_sessions_list())
        else:
            self.render_sessions_list_offline()

    def on_clear_session_clicked(self):
        if self.is_generating:
            self.logger.warning("Нельзя очистить сессию во время генерации.")
            return

        async def _do():
            if not self.is_agent_connected:
                self.logger.warning("Агент OFFLINE: очистка сессии невозможна.")
                return

            try:
                ok = await self.agent.reset_session(self.current_session_id)
                if ok:
                    try:
                        self.output_editbox.clear()
                        self.output_editbox_with_condition.clear()
                        self.metrics_box.clear()
                        self.input_editbox.clear()
                    except Exception:
                        pass

                    await self.refresh_sessions_list()
                    self.logger.success(f"История удалена: {self.current_session_id}")
                else:
                    self.logger.warning("Не удалось удалить историю (agent вернул False).")
            except Exception as e:
                self.logger.warning(f"Ошибка удаления истории: {e}")

        asyncio.get_event_loop().create_task(_do())

    

        
    

"""