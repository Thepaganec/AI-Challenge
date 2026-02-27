import os, json, asyncio, time
import uuid

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QSizePolicy, QProgressBar, QSplitter, QLabel,
    QLineEdit, QPushButton, QComboBox, QDoubleSpinBox, QListWidget, QListWidgetItem,
    QSpinBox
)

from PySide6.QtCore import (Qt, QByteArray, QTimer, QEvent)
from PySide6.QtGui import QTextCursor, QFont

from ui.custom_objects.toggle_switch import ToggleSwitch
from ui.tabs.base_tab import BaseTab
from core.agent.agent_client import AgentClient
from extra.Global import (set_editbox_height)

class ChatTab(BaseTab):
    path = os.path.dirname(__file__)
    file_name = f"{os.path.splitext(os.path.basename(__file__))[0]}.json"
    CONFIG_FILE = os.path.join(path, file_name)

    def __init__(self, logger):
        super().__init__(logger)

        self.agent = AgentClient()

        # --- sessions
        self.current_session_id = str(uuid.uuid4())
        self.sessions_index = {}

        # --- agent connection
        self.is_agent_connected = False
        self.agent_watchdog_task = None

        # --- Служебные
        self.is_generating = False
        self.stop_requested = False
        self.current_task = None

        self.init_content()
        self.load_window_state()

        self.splitter_move_timer = QTimer(self)
        self.splitter_move_timer.setSingleShot(True)

        # ============ СЛУШАЕМ КРИКИ
        self.log_splitter.splitterMoved.connect(self.on_splitter_moved)
        self.vertical_splitter.splitterMoved.connect(self.on_splitter_moved)
        self.splitter_move_timer.timeout.connect(self.save_window_state)
        self.condition_toggle.toggled.connect(self.condition_toggle_changed)

        # Обработка отправки через Enter
        self.input_editbox.installEventFilter(self)

        # --- Модель влияет на доступность temperature
        self.model_selector.currentTextChanged.connect(self.on_model_changed)
        self.on_model_changed(self.model_selector.currentText())

        # --- агент: первичная проверка + вечный watchdog переподключения
        asyncio.get_event_loop().create_task(self.preload_agent_status())
        self.agent_watchdog_task = asyncio.get_event_loop().create_task(self.agent_connection_watchdog())

        # --- наполним список сессий хотя бы текущей, даже если агент оффлайн
        self.render_sessions_list_offline()

    def init_content(self):
        # ============ ОБЪЕКТЫ ВКЛАДКИ
        font = QFont()
        font.setPointSize(13)

        # --- Список сессий (фикс ширина 400, высота меньше на треть)
        self.sessions_list = QListWidget(self)
        self.sessions_list.setFixedWidth(400)
        self.sessions_list.setMinimumHeight(95)  # было ~140, минус треть
        self.sessions_list.itemClicked.connect(self.on_session_clicked)

        self.new_session_button = QPushButton("Новая сессия")
        self.new_session_button.setFixedHeight(34)
        self.new_session_button.clicked.connect(self.on_new_session_clicked)

        self.clear_session_button = QPushButton("Очистить сессию")
        self.clear_session_button.setFixedHeight(34)
        self.clear_session_button.clicked.connect(self.on_clear_session_clicked)

        sessions_buttons = QWidget()
        sessions_buttons_layout = QHBoxLayout(sessions_buttons)
        sessions_buttons_layout.setContentsMargins(0, 0, 0, 0)
        sessions_buttons_layout.setSpacing(6)
        sessions_buttons_layout.addWidget(self.new_session_button)
        sessions_buttons_layout.addWidget(self.clear_session_button)

        session_container = QWidget()
        session_container.setFixedWidth(400)
        session_container.setFixedHeight(140)  # было 200, минус треть
        session_layout = QVBoxLayout(session_container)
        session_layout.setContentsMargins(0, 0, 0, 0)
        session_layout.setSpacing(6)
        session_layout.addWidget(self.sessions_list)
        session_layout.addWidget(sessions_buttons)

        # --- Верхняя панель настроек (модель / эндпоинт / температура)
        self.model_label = QLabel("Модель:")
        self.model_selector = QComboBox()
        self.model_selector.setFixedWidth(260)
        self.model_selector.addItem("gpt-3.5-turbo")
        self.model_selector.addItem("gpt-4o-mini")
        self.model_selector.addItem("gpt-4o")
        self.model_selector.addItem("gpt-5.2-chat-latest")

        self.endpoint_label = QLabel("Эндпоинт:")
        self.endpoint_selector = QComboBox()
        self.endpoint_selector.setFixedWidth(190)
        self.endpoint_selector.addItem("Chat Completions", "chat")
        self.endpoint_selector.addItem("Responses", "responses")

        self.temperature_label = QLabel("temperature:")
        self.temperature_input = QDoubleSpinBox()
        self.temperature_input.setFixedWidth(120)
        self.temperature_input.setDecimals(1)
        self.temperature_input.setSingleStep(0.1)
        self.temperature_input.setRange(0.0, 2.0)
        self.temperature_input.setValue(1.0)

        # --- Новые параметры под temperature
        self.char_limit_label = QLabel("Порог длины (символы):")
        self.char_limit_input = QSpinBox()
        self.char_limit_input.setRange(500, 200000)
        self.char_limit_input.setSingleStep(500)
        self.char_limit_input.setValue(12000)
        self.char_limit_input.setFixedWidth(140)
        self.char_limit_input.valueChanged.connect(self.on_threshold_changed)

        self.keep_last_n_label = QLabel("N последних сообщений (оригинал):")
        self.keep_last_n_input = QSpinBox()
        self.keep_last_n_input.setRange(1, 200)
        self.keep_last_n_input.setSingleStep(1)
        self.keep_last_n_input.setValue(8)
        self.keep_last_n_input.setFixedWidth(140)
        self.keep_last_n_input.valueChanged.connect(self.on_threshold_changed)

        # --- Поле для ввода
        self.input_editbox = QTextEdit()
        self.input_editbox.setFont(font)
        self.input_editbox.setPlaceholderText("Ты можешь попробовать спросить, но не факт, что тебе кто-то ответит...")
        self.input_editbox.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        set_editbox_height(self.input_editbox, 7)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(8)

        # --- Лейблы над окнами вывода (len(new_message) / limit)
        self.plain_len_label = QLabel("0 / 0")
        self.condition_len_label = QLabel("0 / 0")

        # --- Поле для вывода ответа без условий
        self.output_editbox = QTextEdit()
        self.output_editbox.setFont(font)
        self.output_editbox.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.output_editbox.setReadOnly(True)
        set_editbox_height(self.output_editbox, 10)

        # --- Поле для вывода ответа с условиями
        self.output_editbox_with_condition = QTextEdit()
        self.output_editbox_with_condition.setFont(font)
        self.output_editbox_with_condition.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.output_editbox_with_condition.setReadOnly(True)
        set_editbox_height(self.output_editbox_with_condition, 10)

        # --- Тогл для включения условий
        self.condition_toggle = ToggleSwitch()
        self.condition_label = QLabel("Режим запуска с условиями:")
        self.condition_label.setFixedWidth(230)

        # --- Кнопки STOP/CLEAR
        self.stop_button_plain = QPushButton("STOP")
        self.stop_button_plain.setFixedWidth(150)
        self.stop_button_plain.setEnabled(False)
        self.stop_button_plain.clicked.connect(self.stop_generation_plain)

        self.clear_button_plain = QPushButton("CLEAR")
        self.clear_button_plain.setFixedWidth(150)
        self.clear_button_plain.setEnabled(False)
        self.clear_button_plain.clicked.connect(self.clear_output_editbox)
        self.output_editbox.textChanged.connect(self.set_enable_clear_button_plain)

        self.stop_button_condition = QPushButton("STOP")
        self.stop_button_condition.setFixedWidth(150)
        self.stop_button_condition.setEnabled(False)
        self.stop_button_condition.clicked.connect(self.stop_generation_condition)

        self.clear_button_condition = QPushButton("CLEAR")
        self.clear_button_condition.setFixedWidth(150)
        self.clear_button_condition.setEnabled(False)
        self.clear_button_condition.clicked.connect(self.clear_output_editbox_with_condition)
        self.output_editbox_with_condition.textChanged.connect(self.set_enable_clear_button_condition)

        # --- Поля условий (правая панель)
        self.format_label = QLabel("Формат ответа:")
        self.format_label.setFixedWidth(230)
        self.format_input = QLineEdit()
        self.format_input.setFixedWidth(350)
        self.format_input.setPlaceholderText("Например: Ровно 3 пункта, без вступления.")

        self.length_label = QLabel("Ограничение длины (слова/символы):")
        self.length_label.setFixedWidth(230)
        self.length_input = QLineEdit()
        self.length_input.setFixedWidth(350)
        self.length_input.setPlaceholderText("Например: Не более 60 слов.")

        self.stop_seq_label = QLabel("Stop sequence (строка завершения):")
        self.stop_seq_label.setFixedWidth(230)
        self.stop_seq_input = QLineEdit()
        self.stop_seq_input.setFixedWidth(350)
        self.stop_seq_input.setPlaceholderText("Например: ###END###")
        self.stop_seq_input.setText("###END###")

        self.max_tokens_label = QLabel("max_tokens (через API):")
        self.max_tokens_label.setFixedWidth(230)
        self.max_tokens_input = QLineEdit()
        self.max_tokens_input.setFixedWidth(350)
        self.max_tokens_input.setPlaceholderText("Например: 200")
        self.max_tokens_input.setText("200")

        # --- Окно результатов замеров
        self.metrics_label = QLabel("Результаты замеров (Day 5):")
        self.metrics_box = QTextEdit()
        self.metrics_box.setReadOnly(True)
        self.metrics_box.setPlaceholderText(
            "Здесь будет появляться результат каждой попытки:\n"
            "TTFT / Total time / Tokens / Cost / Model / Endpoint / Temperature..."
        )
        self.metrics_box.setMinimumHeight(220)

        # --- НОВОЕ: настройки суммаризации + окно вывода суммаризации
        self.summary_model_label = QLabel("Модель для суммаризации:")
        self.summary_model_selector = QComboBox()
        self.summary_model_selector.setFixedWidth(260)
        self.summary_model_selector.addItem("gpt-3.5-turbo")
        self.summary_model_selector.addItem("gpt-4o-mini")
        self.summary_model_selector.addItem("gpt-4o")
        self.summary_model_selector.addItem("gpt-5.2-chat-latest")

        self.summary_endpoint_label = QLabel("Эндпоинт для суммаризации:")
        self.summary_endpoint_selector = QComboBox()
        self.summary_endpoint_selector.setFixedWidth(190)
        self.summary_endpoint_selector.addItem("Chat Completions", "chat")
        self.summary_endpoint_selector.addItem("Responses", "responses")

        self.summary_output_label = QLabel("History_summary (из сессии):")
        self.summary_output_box = QTextEdit()
        self.summary_output_box.setReadOnly(True)
        self.summary_output_box.setMinimumHeight(180)
        self.summary_output_box.setPlaceholderText("Здесь будет появляться суммаризация истории...")

        # ============ РАССТАНОВКА ЭЛЕМЕНТОВ
        tab_layout = QVBoxLayout(self.top_widget)
        tab_layout.setContentsMargins(0, 0, 0, 0)
        tab_layout.setSpacing(8)

        # --- Header wrap: слева сессии, справа параметры (параметры прижаты вправо)
        header_wrap = QWidget()
        header_wrap_layout = QHBoxLayout(header_wrap)
        header_wrap_layout.setContentsMargins(0, 0, 25, 0)
        header_wrap_layout.setSpacing(12)

        # контейнер параметров: лейбл слева, поле справа (2 колонки)
        params_container = QWidget()
        params_layout = QVBoxLayout(params_container)
        params_layout.setContentsMargins(0, 0, 0, 0)
        params_layout.setSpacing(6)

        def _row(lbl: QLabel, widget: QWidget):
            r = QWidget()
            r_l = QHBoxLayout(r)
            r_l.setContentsMargins(0, 0, 0, 0)
            r_l.setSpacing(8)
            r_l.addWidget(lbl, alignment=Qt.AlignLeft)
            r_l.addStretch()
            r_l.addWidget(widget, alignment=Qt.AlignRight)
            return r

        params_layout.addWidget(_row(self.model_label, self.model_selector))
        params_layout.addWidget(_row(self.endpoint_label, self.endpoint_selector))
        params_layout.addWidget(_row(self.temperature_label, self.temperature_input))
        params_layout.addWidget(_row(self.char_limit_label, self.char_limit_input))
        params_layout.addWidget(_row(self.keep_last_n_label, self.keep_last_n_input))
        params_layout.addStretch()

        header_wrap_layout.addWidget(session_container, alignment=Qt.AlignTop)
        header_wrap_layout.addStretch()
        header_wrap_layout.addWidget(params_container, alignment=Qt.AlignTop | Qt.AlignRight)

        # --- Ввод (выровнен по верху)
        input_container = QWidget()
        input_container.setFixedWidth(400)
        input_layout = QVBoxLayout(input_container)
        input_layout.setContentsMargins(0, 0, 0, 0)
        input_layout.setSpacing(5)
        input_layout.addWidget(self.input_editbox, alignment=Qt.AlignTop)
        input_layout.addWidget(self.progress_bar, alignment=Qt.AlignTop)

        # --- Выводы (2 окна)
        outbox_plain_buttons_container = QWidget()
        outbox_plain_layout = QHBoxLayout(outbox_plain_buttons_container)
        outbox_plain_layout.setContentsMargins(0, 0, 0, 0)
        outbox_plain_layout.addWidget(self.stop_button_plain, alignment=Qt.AlignLeft)
        outbox_plain_layout.addWidget(self.clear_button_plain, alignment=Qt.AlignRight)

        plain_output_container = QWidget()
        plain_output_layout = QVBoxLayout(plain_output_container)
        plain_output_layout.setContentsMargins(0, 0, 0, 0)
        plain_output_layout.setSpacing(4)
        plain_output_layout.addWidget(self.plain_len_label, alignment=Qt.AlignLeft)
        plain_output_layout.addWidget(self.output_editbox)
        plain_output_layout.addWidget(outbox_plain_buttons_container)

        outbox_condition_buttons_container = QWidget()
        outbox_condition_layout = QHBoxLayout(outbox_condition_buttons_container)
        outbox_condition_layout.setContentsMargins(0, 0, 0, 0)
        outbox_condition_layout.addWidget(self.stop_button_condition, alignment=Qt.AlignLeft)
        outbox_condition_layout.addWidget(self.clear_button_condition, alignment=Qt.AlignRight)

        condition_output_container = QWidget()
        condition_output_layout = QVBoxLayout(condition_output_container)
        condition_output_layout.setContentsMargins(0, 0, 0, 0)
        condition_output_layout.setSpacing(4)
        condition_output_layout.addWidget(self.condition_len_label, alignment=Qt.AlignLeft)
        condition_output_layout.addWidget(self.output_editbox_with_condition)
        condition_output_layout.addWidget(outbox_condition_buttons_container)

        output_container = QWidget()
        output_container.setFixedWidth(820)
        output_layout = QHBoxLayout(output_container)
        output_layout.setContentsMargins(0, 0, 0, 0)
        output_layout.setSpacing(8)
        output_layout.addWidget(plain_output_container)
        output_layout.addWidget(condition_output_container)

        union_container = QWidget()
        union_layout = QVBoxLayout(union_container)
        union_layout.setContentsMargins(0, 0, 0, 0)
        union_layout.setSpacing(8)
        union_layout.addWidget(input_container, alignment=Qt.AlignTop | Qt.AlignHCenter)
        union_layout.addWidget(output_container, alignment=Qt.AlignTop | Qt.AlignHCenter)
        union_layout.addStretch()

        left_panel_container = QWidget()
        left_panel_layout = QVBoxLayout(left_panel_container)
        left_panel_layout.setContentsMargins(0, 0, 0, 0)
        left_panel_layout.addWidget(header_wrap)
        left_panel_layout.addWidget(union_container, alignment=Qt.AlignTop | Qt.AlignLeft)
        left_panel_layout.addStretch()

        # --- Правая панель: начинается с самого верха
        right_panel_container = QWidget()
        right_panel_layout = QVBoxLayout(right_panel_container)
        right_panel_layout.setContentsMargins(0, 0, 0, 0)
        right_panel_layout.setSpacing(8)

        condition_toggle_layout = QHBoxLayout()
        condition_toggle_layout.setContentsMargins(0, 0, 0, 0)
        condition_toggle_layout.addWidget(self.condition_label, alignment=Qt.AlignLeft)
        condition_toggle_layout.addWidget(self.condition_toggle, alignment=Qt.AlignLeft)
        condition_toggle_layout.addStretch()

        format_layout = QHBoxLayout()
        format_layout.setContentsMargins(0, 0, 0, 0)
        format_layout.addWidget(self.format_label, alignment=Qt.AlignLeft)
        format_layout.addWidget(self.format_input, alignment=Qt.AlignLeft)

        length_layout = QHBoxLayout()
        length_layout.setContentsMargins(0, 0, 0, 0)
        length_layout.addWidget(self.length_label, alignment=Qt.AlignLeft)
        length_layout.addWidget(self.length_input, alignment=Qt.AlignLeft)

        stop_seq_layout = QHBoxLayout()
        stop_seq_layout.setContentsMargins(0, 0, 0, 0)
        stop_seq_layout.addWidget(self.stop_seq_label, alignment=Qt.AlignLeft)
        stop_seq_layout.addWidget(self.stop_seq_input, alignment=Qt.AlignLeft)

        max_tokens_layout = QHBoxLayout()
        max_tokens_layout.setContentsMargins(0, 0, 0, 0)
        max_tokens_layout.addWidget(self.max_tokens_label, alignment=Qt.AlignLeft)
        max_tokens_layout.addWidget(self.max_tokens_input, alignment=Qt.AlignLeft)

        right_panel_layout.addLayout(condition_toggle_layout)
        right_panel_layout.addLayout(format_layout)
        right_panel_layout.addLayout(length_layout)
        right_panel_layout.addLayout(stop_seq_layout)
        right_panel_layout.addLayout(max_tokens_layout)

        right_panel_layout.addSpacing(6)
        right_panel_layout.addWidget(self.metrics_label)
        right_panel_layout.addWidget(self.metrics_box)

        # --- НОВОЕ: блок суммаризации под логами
        right_panel_layout.addSpacing(8)
        right_panel_layout.addWidget(self.summary_model_label)
        right_panel_layout.addWidget(self.summary_model_selector)
        right_panel_layout.addWidget(self.summary_endpoint_label)
        right_panel_layout.addWidget(self.summary_endpoint_selector)
        right_panel_layout.addWidget(self.summary_output_label)
        right_panel_layout.addWidget(self.summary_output_box)

        right_panel_layout.addStretch()

        # --- Сплиттер: слева чат, справа параметры/метрики/summary
        self.vertical_splitter = QSplitter(Qt.Horizontal)
        self.vertical_splitter.addWidget(left_panel_container)
        self.vertical_splitter.addWidget(right_panel_container)

        tab_layout.addWidget(self.vertical_splitter)

        # сразу обновим лейблы лимитов (порог поменяется/перезапуск)
        self.on_threshold_changed()
    
    def on_threshold_changed(self):
        try:
            limit = int(self.char_limit_input.value())
        except Exception:
            limit = 0

        # длина new_message обновляется только при отправке,
        # но порог/подписи должны обновляться сразу при изменении порога.
        # Поэтому тут меняем только знаменатель.
        try:
            left_plain = self.plain_len_label.text().split("/")[0].strip()
            left_cond = self.condition_len_label.text().split("/")[0].strip()
        except Exception:
            left_plain, left_cond = "0", "0"

        self.plain_len_label.setText(f"{left_plain} / {limit}")
        self.condition_len_label.setText(f"{left_cond} / {limit}")

    def render_sessions_list_offline(self):
        try:
            self.sessions_list.blockSignals(True)
            self.sessions_list.clear()

            label = f"{self.current_session_id} — (текущая, новая)"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, self.current_session_id)
            self.sessions_list.addItem(item)
        finally:
            try:
                self.sessions_list.blockSignals(False)
            except Exception:
                pass

    async def preload_agent_status(self):
        try:
            self.logger.info("Подключение к агенту (локальный сервер)...")
            ok = await self.agent.ping()
            self.is_agent_connected = bool(ok)

            if self.is_agent_connected:
                self.logger.success("Агент найден: подключение успешно")
                await self.refresh_sessions_list()
            else:
                self.logger.warning("Агент не отвечает. Запусти agent_server.py перед запуском UI.")
        except Exception as e:
            self.is_agent_connected = False
            self.logger.warning(f"Не удалось подключиться к агенту: {e}")

    async def agent_connection_watchdog(self):
        while True:
            try:
                if not self.is_agent_connected:
                    self.logger.warning("Агент OFFLINE: попытка подключиться к серверу...")
                    ok = await self.agent.ping()

                    if ok:
                        self.is_agent_connected = True
                        self.logger.success("Агент ONLINE: соединение восстановлено")
                        await self.refresh_sessions_list()
            except Exception as e:
                self.is_agent_connected = False
                self.logger.warning(f"Ошибка проверки агента: {e}")

            await asyncio.sleep(5)

    async def refresh_sessions_list(self):
        if not self.is_agent_connected:
            self.render_sessions_list_offline()
            return

        try:
            sessions = await self.agent.list_sessions()
        except Exception as e:
            self.logger.warning(f"Не удалось получить список сессий: {e}")
            self.render_sessions_list_offline()
            return

        self.sessions_list.blockSignals(True)
        self.sessions_list.clear()
        self.sessions_index = {}

        found_current = False

        for s in sessions:
            sid = (s.get("session_id") or "").strip()
            title = (s.get("title") or "").strip()
            if not sid:
                continue

            if sid == self.current_session_id:
                found_current = True

            label = f"{sid} — {title or 'Без темы'}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, sid)
            self.sessions_list.addItem(item)
            self.sessions_index[sid] = title or "Без темы"

        if not found_current:
            label = f"{self.current_session_id} — (текущая, новая)"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, self.current_session_id)
            self.sessions_list.insertItem(0, item)
            self.sessions_index[self.current_session_id] = "(текущая, новая)"

        self.sessions_list.blockSignals(False)

    async def preload_pricing(self):
        try:
            self.logger.info("Загрузка тарифов ProxyAPI (pricing/list)...")
            table = await self.gpt.get_pricing_rub_per_1m()
            self.logger.success(f"Тарифы загружены: {len(table)} моделей")
        except Exception as e:
            self.logger.warning(f"Не удалось загрузить тарифы ProxyAPI: {e}")
    
    def on_session_clicked(self, item: QListWidgetItem):
        sid = item.data(Qt.UserRole)
        if not sid:
            return

        self.current_session_id = str(sid)
        asyncio.get_event_loop().create_task(self.load_session_to_ui(self.current_session_id))

    async def load_session_to_ui(self, session_id: str):
        if not self.is_agent_connected:
            self.logger.warning("Агент OFFLINE: не могу загрузить историю")
            return

        try:
            session = await self.agent.get_session(session_id)
        except Exception as e:
            self.logger.warning(f"Не удалось загрузить сессию {session_id}: {e}")
            return

        if not session:
            return

        history = session.get("history")
        messages = session.get("messages")

        if not isinstance(history, dict):
            history = {}

        if not isinstance(messages, list):
            messages = []

        # --- подтягиваем history_summary
        history_summary = session.get("history_summary") or ""
        try:
            self.summary_output_box.setPlainText(str(history_summary))
        except Exception:
            pass

        try:
            self.output_editbox.clear()
            self.output_editbox_with_condition.clear()
            self.metrics_box.clear()
        except Exception:
            pass

        last_turn = None

        if history:
            try:
                keys = sorted(history.keys(), key=lambda x: int(x))
            except Exception:
                keys = list(history.keys())

            for k in keys:
                turn = history.get(k) or {}
                user_text = turn.get("user_text") or ""
                assistant_text = turn.get("assistant_text") or ""

                if user_text:
                    self.output_editbox.append("Ты: " + user_text)
                    self.output_editbox.append("")
                if assistant_text:
                    self.output_editbox.append("GPT: " + assistant_text)
                    self.output_editbox.append("")

                model = (turn.get("model") or "N/A").strip()
                endpoint = (turn.get("endpoint") or "N/A").strip()

                r = int(turn.get("r_prompt_total") or 0)
                r_prev = int(turn.get("r_prev_prompt_total") or 0)
                c = int(turn.get("c_completion") or 0)

                current_message_tokens = int(turn.get("current_message_tokens") or 0)
                total_tokens_call = int(turn.get("total_tokens_call") or 0)

                cost_rub = turn.get("cost_rub", None)
                cost_str = f"{float(cost_rub):.4f} ₽" if isinstance(cost_rub, (int, float)) else "N/A"

                temp_val = turn.get("temperature", None)
                if isinstance(temp_val, (int, float)):
                    temp_str = f"{float(temp_val)}"
                else:
                    temp_str = "locked(1.0)"

                result_line = (
                    f"Model={model} | "
                    f"Endpoint={endpoint} | "
                    f"Temp={temp_str} | "
                    f"TTFT=N/A | "
                    f"Total=N/A | "
                    f"prompt(r)={r} (prev_r={r_prev}) | "
                    f"completion(c)={c} | "
                    f"current_message_tokens={current_message_tokens} | "
                    f"total_tokens={total_tokens_call} | "
                    f"Cost={cost_str}"
                )

                try:
                    self.metrics_box.append(result_line)
                except Exception:
                    pass

                last_turn = turn
        else:
            for m in messages:
                role = (m.get("role") or "").strip()
                content = m.get("content") or ""

                if role == "user":
                    prefix = "Ты: "
                elif role == "assistant":
                    prefix = "GPT: "
                else:
                    prefix = f"{role}: " if role else ""

                self.output_editbox.append(prefix + content)
                self.output_editbox.append("")

        if isinstance(last_turn, dict):
            last_model = (last_turn.get("model") or "").strip()
            last_endpoint = (last_turn.get("endpoint") or "").strip()
            last_temp = last_turn.get("temperature", None)

            if last_model:
                idx = self.model_selector.findText(last_model)
                if idx >= 0:
                    self.model_selector.setCurrentIndex(idx)

            if last_endpoint:
                idx2 = self.endpoint_selector.findData(last_endpoint)
                if idx2 >= 0:
                    self.endpoint_selector.setCurrentIndex(idx2)

            if self.temperature_input.isEnabled() and isinstance(last_temp, (int, float)):
                try:
                    self.temperature_input.setValue(float(last_temp))
                except Exception:
                    pass

        # обновим знаменатель в лейблах (порог)
        self.on_threshold_changed()

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

    def on_model_changed(self, model_text: str):
        model_text = (model_text or "").strip()

        # Для openai/gpt-5.2-chat-latest ProxyAPI запрещает temperature != 1
        is_gpt52_locked = (model_text == "gpt-5.2-chat-latest")

        self.temperature_input.setEnabled(not is_gpt52_locked)

        if is_gpt52_locked:
            # Сбрасываем в 1.0, чтобы было очевидно, что иначе нельзя
            self.temperature_input.setValue(1.0)
            self.logger.warning("Для gpt-5.2-chat-latest temperature заблокирована ProxyAPI. Установлено 1.0.")
        else:
            self.logger.info(f"Выбрана модель {model_text}. temperature доступна.")

    def set_enable_clear_button_plain(self):
        state = True if self.output_editbox.toPlainText().strip() != "" else False
        self.clear_button_plain.setEnabled(state) 

    def set_enable_clear_button_condition(self):
        state = True if self.output_editbox_with_condition.toPlainText().strip() != "" else False
        self.clear_button_condition.setEnabled(state) 

    def clear_output_editbox(self):
        self.output_editbox.clear()
        self.set_enable_clear_button_plain()

    def clear_output_editbox_with_condition(self):
        self.output_editbox_with_condition.clear()
        self.set_enable_clear_button_condition()

    def condition_toggle_changed(self, state: bool):
        self.logger.info(f"Значение condition_toggle изменилось на {state}")
        self.history = []
        self.output_editbox_with_condition.clear() if state else self.output_editbox.clear()


    # ========= Enter отправляет, Shift+Enter перенос строки =========
    def eventFilter(self, obj, event):
        if obj is self.input_editbox and event.type() == QEvent.KeyPress:
            key = event.key()
            mods = event.modifiers()

            if key in (Qt.Key_Return, Qt.Key_Enter):
                # Shift+Enter — оставить стандартное поведение (новая строка)
                if mods & Qt.ShiftModifier:
                    return False

                # Enter — отправить
                self.on_send_message()
                return True

        return super().eventFilter(obj, event)

    def set_loading(self, is_loading: bool):
        if is_loading:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)

    def on_send_message(self):
        if self.is_generating:
            self.logger.warning("Модель ещё отвечает — подожди.")
            return

        text = self.input_editbox.toPlainText().strip()
        if not text:
            self.logger.warning("Отсутствует текст для отправки!")
            return

        self.input_editbox.clear()

        use_conditions = self.condition_toggle.isChecked()
        target_output = self.output_editbox_with_condition if use_conditions else self.output_editbox

        self.stop_button_plain.setEnabled(False)
        self.stop_button_condition.setEnabled(False)

        if use_conditions:
            self.stop_button_condition.setEnabled(True)
        else:
            self.stop_button_plain.setEnabled(True)

        target_output.append(f"Ты: {text} \n")
        target_output.append("GPT: ")

        self.set_loading(True)

        # --- условия
        if use_conditions:
            fmt = self.format_input.text().strip()
            length_rule = self.length_input.text().strip()
            stop_seq = self.stop_seq_input.text().strip()

            instructions = []
            if fmt:
                instructions.append(f"Формат ответа: {fmt}")
            if length_rule:
                instructions.append(f"Ограничение длины: {length_rule}")
            if stop_seq:
                instructions.append(f"Условие завершения: в конце добавь строку {stop_seq} и после неё ничего не пиши.")

            controlled_text = text
            if instructions:
                controlled_text = text + "\n\n" + "\n".join(instructions)

            try:
                max_tokens = int(self.max_tokens_input.text().strip())
            except Exception:
                max_tokens = 200
                self.logger.warning("max_tokens задан неверно, использую 200.")
        else:
            controlled_text = text
            max_tokens = 800

        # --- параметры “сжатия”
        try:
            char_limit = int(self.char_limit_input.value())
        except Exception:
            char_limit = 12000

        try:
            keep_last_n = int(self.keep_last_n_input.value())
        except Exception:
            keep_last_n = 8

        summary_model = self.summary_model_selector.currentText().strip()
        summary_endpoint = self.summary_endpoint_selector.currentData()

        self.stop_requested = False
        self.is_generating = True
        self.current_task = asyncio.create_task(
            self.ask_and_stream_answer(
                controlled_text,
                target_output,
                use_conditions,
                max_tokens,
                char_limit,
                keep_last_n,
                summary_model,
                summary_endpoint,
            )
        )

    async def ask_and_stream_answer(
        self,
        user_text: str,
        target_output: QTextEdit,
        use_conditions: bool,
        max_tokens: int,
        char_limit: int,
        keep_last_n: int,
        summary_model: str,
        summary_endpoint: str,
    ):
        self.logger.info("Отправка запроса в агент")

        stop_seq = self.stop_seq_input.text().strip() if use_conditions else ""
        buffer_text = ""

        gen = None

        selected_model = self.model_selector.currentText().strip()
        selected_endpoint = self.endpoint_selector.currentData()

        selected_temperature = None
        if self.temperature_input.isEnabled():
            selected_temperature = float(self.temperature_input.value())

        t0 = time.perf_counter()
        ttft_sec = None
        got_first_chunk = False

        error_text = None

        try:
            if not self.is_agent_connected:
                self.logger.warning("Агент OFFLINE: проверяю доступность перед отправкой...")
                try:
                    ok = await self.agent.ping()
                except Exception:
                    ok = False

                self.is_agent_connected = bool(ok)

                if not self.is_agent_connected:
                    target_output.append("\n[Ошибка] Агент не запущен или недоступен (server OFFLINE).\n")
                    return

            cursor = target_output.textCursor()
            cursor.movePosition(QTextCursor.End)
            target_output.setTextCursor(cursor)

            gen = self.agent.stream_chat(
                user_text=user_text,
                model=selected_model,
                endpoint=selected_endpoint,
                max_tokens=max_tokens,
                temperature=selected_temperature,
                session_id=self.current_session_id,
                char_limit=int(char_limit),
                keep_last_n=int(keep_last_n),
                summary_model=str(summary_model or "").strip(),
                summary_endpoint=str(summary_endpoint or "chat"),
            )

            async for chunk in gen:
                if self.stop_requested:
                    break

                if (not got_first_chunk) and chunk:
                    got_first_chunk = True
                    ttft_sec = time.perf_counter() - t0

                target_output.insertPlainText(chunk)
                target_output.moveCursor(QTextCursor.End)
                target_output.ensureCursorVisible()

                if use_conditions and stop_seq:
                    buffer_text += chunk
                    if stop_seq in buffer_text:
                        break

            target_output.append("")

        except asyncio.CancelledError:
            try:
                target_output.append("\n[Остановлено пользователем]\n")
            except Exception:
                pass
            raise

        except Exception as e:
            error_text = str(e)

            is_proxyapi_error = ("ProxyAPI error:" in error_text) or ("HTTP 400" in error_text) or ("ContextWindowExceededError" in error_text)

            is_connection_error = isinstance(
                e,
                (
                    ConnectionError,
                    ConnectionRefusedError,
                    ConnectionResetError,
                    BrokenPipeError,
                    asyncio.IncompleteReadError,
                    asyncio.TimeoutError,
                    OSError,
                ),
            )

            if not is_proxyapi_error:
                low = error_text.lower()
                if ("connection reset" in low) or ("broken pipe" in low) or ("connection refused" in low) or ("cannot connect" in low):
                    is_connection_error = True

            if is_connection_error and (not is_proxyapi_error):
                if self.is_agent_connected:
                    self.is_agent_connected = False
                    self.logger.error("Соединение с агентом потеряно (server OFFLINE)")

            self.logger.error_handler(e, context="ChatTab -> ask_and_stream_answer")
            target_output.append(f"\n[Ошибка] {e}\n")

        finally:
            if gen is not None:
                try:
                    await gen.aclose()
                except Exception:
                    pass

            total_sec = time.perf_counter() - t0

            usage = getattr(self.agent, "last_usage", None) or {}
            prompt_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            total_tokens_call = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))

            cost_rub = getattr(self.agent, "last_cost_rub", None)

            ms = getattr(self.agent, "last_message_stats", None) or {}
            r_prev_prompt_total = int(ms.get("r_prev_prompt_total") or 0)
            current_message_tokens = int(ms.get("current_message_tokens") or 0)

            # --- NEW stats
            new_message_len = int(ms.get("new_message_len") or 0)
            char_limit_used = int(ms.get("char_limit") or char_limit)
            history_summarized = bool(ms.get("history_summarized") or False)
            history_summary_text = ms.get("history_summary") or ""

            # --- обновим summary в UI если агент прислал
            if isinstance(history_summary_text, str) and history_summary_text.strip():
                try:
                    self.summary_output_box.setPlainText(history_summary_text)
                except Exception:
                    pass

            # --- обновим лейблы длины (ТОЛЬКО при отправке — как ты и хотел)
            try:
                if use_conditions:
                    self.condition_len_label.setText(f"{new_message_len} / {char_limit_used}")
                else:
                    self.plain_len_label.setText(f"{new_message_len} / {char_limit_used}")
            except Exception:
                pass

            ttft_str = f"{ttft_sec:.3f}s" if isinstance(ttft_sec, (int, float)) else "N/A"
            temp_str = f"{selected_temperature}" if selected_temperature is not None else "locked(1.0)"
            cost_str = f"{cost_rub:.4f} ₽" if isinstance(cost_rub, (int, float)) else "N/A"

            result_line = (
                f"Model={selected_model} | "
                f"Endpoint={selected_endpoint} | "
                f"Temp={temp_str} | "
                f"TTFT={ttft_str} | "
                f"Total={total_sec:.3f}s | "
                f"prompt(r)={prompt_tokens} (prev_r={r_prev_prompt_total}) | "
                f"completion(c)={completion_tokens} | "
                f"current_message_tokens={current_message_tokens} | "
                f"total_tokens={total_tokens_call} | "
                f"Cost={cost_str} | "
                f"new_message_len={new_message_len}/{char_limit_used} | "
                f"summarized={history_summarized}"
            )

            if error_text:
                short_err = error_text.replace("\n", " ")
                if len(short_err) > 180:
                    short_err = short_err[:180] + "..."
                result_line += f" | ERROR={short_err}"

            try:
                self.metrics_box.append(result_line)
            except Exception:
                pass

            self.is_generating = False
            self.current_task = None
            self.set_loading(False)

            self.stop_button_plain.setEnabled(False)
            self.stop_button_condition.setEnabled(False)

            self.logger.success("Ответ получен")

            if self.is_agent_connected:
                asyncio.get_event_loop().create_task(self.refresh_sessions_list())

    def stop_generation_plain(self):
        self.stop_generation()

    def stop_generation_condition(self):
        self.stop_generation()

    def stop_generation(self):
        if not self.is_generating:
            return

        self.stop_requested = True

        if self.current_task is not None and not self.current_task.done():
            self.current_task.cancel()

        self.stop_button_plain.setEnabled(False)
        self.stop_button_condition.setEnabled(False)
        self.set_loading(False)

        self.logger.warning("Стрим остановлен пользователем.")

    def on_splitter_moved(self):
        self.splitter_move_timer.start(300)

    def save_window_state(self):
        try:
            state = {}
            if hasattr(self, "log_splitter"):
                state["log_splitter"] = self.log_splitter.saveState().toHex().data().decode()

            if hasattr(self, "vertical_splitter"):
                state["vertical_splitter"] = self.vertical_splitter.saveState().toHex().data().decode()

            with open(self.CONFIG_FILE, "w") as f:
                json.dump(state, f)

        except Exception as e:
            self.logger.error_handler(e, context="ChatTab -> save_window_state")
            return

    def load_window_state(self):
        if not os.path.exists(self.CONFIG_FILE):
            return
        try:
            with open(self.CONFIG_FILE, "r") as f:
                state = json.load(f)

            if "log_splitter" in state:
                try:
                    splitter_state = QByteArray.fromHex(str(state["log_splitter"]).encode())
                    self.log_splitter.restoreState(splitter_state)
                except Exception as e:
                    self.logger.error(f"Ошибка восстановления состояния log_splitter для вкладки \"Chat_tab\": {e}")
                    return
                
            if "vertical_splitter" in state:
                try:
                    splitter_state = QByteArray.fromHex(str(state["vertical_splitter"]).encode())
                    self.vertical_splitter.restoreState(splitter_state)
                except Exception as e:
                    self.logger.error(f"Ошибка восстановления состояния vertical_splitter вкладки \"Chat_tab\": {e}")
                    return

        except Exception as e:
            self.logger.error(f"Ошибка загрузки состояния окна для вкладки \"Chat_tab\": {e}")
            return

        self.logger.debug("Состояние вкладки загружено")
