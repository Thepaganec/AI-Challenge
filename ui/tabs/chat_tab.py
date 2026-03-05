import os, json, asyncio, time
import uuid

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QSizePolicy, QProgressBar, QLabel,
    QPushButton, QComboBox, QDoubleSpinBox, QListWidget, QListWidgetItem, QSpinBox,
    QLineEdit, QSplitter, QGroupBox
)
from PySide6.QtCore import Qt, QByteArray, QTimer, QEvent
from PySide6.QtGui import QTextCursor, QFont

from ui.tabs.base_tab import BaseTab
from ui.tabs.metrics_memory_tab import MetricsMemoryTab
from core.agent.agent_client import AgentClient
from extra.global_utils import set_editbox_height


class ChatTab(BaseTab):
    path = os.path.dirname(__file__)
    file_name = f"{os.path.splitext(os.path.basename(__file__))[0]}.json"
    CONFIG_FILE = os.path.join(path, file_name)

    # === Инициализация и сборка UI ===

    # Инициализирует состояние текущей сессии/ветки, связывает контролы с обработчиками и строит полный интерфейс чат-вкладки.

    # Инициализирует внутреннее состояние объекта и связывает зависимости, которые будут использоваться остальными методами класса.

    def __init__(self, logger, metrics_memory_tab: MetricsMemoryTab | None = None):
        super().__init__(logger)

        self.agent = AgentClient()

        # sessions
        self.current_session_id = str(uuid.uuid4())
        self.is_agent_connected = False

        # branching
        self.current_branch_id = "main"

        # stream state
        self.is_generating = False
        self.stop_requested = False
        self.current_task = None
        self.pending_memory_write = None
        self._memory_layer_hint_logged = False

        self.metrics_memory_tab = metrics_memory_tab or MetricsMemoryTab()
        self.metrics_box = self.metrics_memory_tab.metrics_box
        self.memory_layer_selector = self.metrics_memory_tab.memory_layer_selector
        self.memory_key_input = self.metrics_memory_tab.memory_key_input
        self.memory_value_input = self.metrics_memory_tab.memory_value_input
        self.save_memory_btn = self.metrics_memory_tab.save_memory_btn
        self.memory_box = self.metrics_memory_tab.memory_box
        self.save_memory_btn.clicked.connect(self.on_save_memory_clicked)
        self.profile_selector = self.metrics_memory_tab.profile_selector
        self.profile_description_input = self.metrics_memory_tab.profile_description_input
        self.profile_use_toggle = self.metrics_memory_tab.profile_use_toggle
        self.save_profile_btn = self.metrics_memory_tab.save_profile_btn
        self.delete_profile_btn = self.metrics_memory_tab.delete_profile_btn
        self.profile_none_label = "Без профиля"
        self.active_profile_name = ""
        self._profile_selector_changing = False
        self.task_state_cache: dict = {}
        self._task_state_signature: str = ""

        self.profile_selector.currentIndexChanged.connect(self.on_profile_selected)
        self.profile_selector.currentTextChanged.connect(self.on_profile_text_changed)
        self.save_profile_btn.clicked.connect(self.on_save_profile_clicked)
        self.delete_profile_btn.clicked.connect(self.on_delete_profile_clicked)
        self.profile_use_toggle.toggled.connect(self.on_profile_toggle_changed)

        self.init_content()
        self.load_window_state()

        self.splitter_move_timer = QTimer(self)
        self.splitter_move_timer.setSingleShot(True)
        self.log_splitter.splitterMoved.connect(self.on_splitter_moved)
        self.vertical_splitter.splitterMoved.connect(self.on_splitter_moved)
        self.splitter_move_timer.timeout.connect(self.save_window_state)

        # Enter send
        self.input_editbox.installEventFilter(self)

        asyncio.get_event_loop().create_task(self.preload_agent_status())
        asyncio.get_event_loop().create_task(self.refresh_sessions_list_offline_first())

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def init_content(self):
        font = QFont()
        font.setPointSize(13)

        # ===== left: sessions + chat =====
        self.sessions_list = QListWidget(self)
        self.sessions_list.setFixedWidth(420)
        self.sessions_list.setMinimumHeight(120)
        self.sessions_list.itemClicked.connect(self.on_session_clicked)

        self.new_session_button = QPushButton("Новая сессия")
        self.new_session_button.clicked.connect(self.on_new_session_clicked)

        self.clear_session_button = QPushButton("Очистить сессию")
        self.clear_session_button.clicked.connect(self.on_clear_session_clicked)

        sessions_buttons = QWidget()
        sb_l = QHBoxLayout(sessions_buttons)
        sb_l.setContentsMargins(0, 0, 0, 0)
        sb_l.setSpacing(6)
        sb_l.addWidget(self.new_session_button)
        sb_l.addWidget(self.clear_session_button)

        session_box = QGroupBox("Сессии")
        session_box.setFixedWidth(420)
        session_layout = QVBoxLayout(session_box)
        session_layout.setContentsMargins(8, 8, 8, 8)
        session_layout.setSpacing(6)
        session_layout.addWidget(self.sessions_list)
        session_layout.addWidget(sessions_buttons)

        self.task_state_box = QTextEdit()
        self.task_state_box.setReadOnly(True)
        self.task_state_box.setMinimumHeight(120)
        self.task_state_box.setPlaceholderText("Task state появится после генерации плана.")

        self.task_confirm_btn = QPushButton("Confirm Plan")
        self.task_confirm_btn.clicked.connect(self.on_task_confirm_clicked)
        self.task_confirm_btn.setToolTip("Подтверждение плана выполняется сообщением пользователя (например: 'да').")
        self.task_confirm_btn.setVisible(False)

        self.task_pause_btn = QPushButton("Pause")
        self.task_pause_btn.clicked.connect(self.on_task_pause_clicked)

        self.task_resume_btn = QPushButton("Resume")
        self.task_resume_btn.clicked.connect(self.on_task_resume_clicked)

        self.task_delete_btn = QPushButton("Delete Task")
        self.task_delete_btn.clicked.connect(self.on_task_delete_clicked)

        self.task_next_btn = QPushButton("Next Step")
        self.task_next_btn.clicked.connect(self.on_task_next_clicked)
        self.task_next_btn.setToolTip("Переходы этапов выполняются серверным оркестратором автоматически.")
        self.task_next_btn.setVisible(False)

        task_buttons_row = QWidget()
        tb_l = QHBoxLayout(task_buttons_row)
        tb_l.setContentsMargins(0, 0, 0, 0)
        tb_l.setSpacing(6)
        tb_l.addWidget(self.task_confirm_btn)
        tb_l.addWidget(self.task_pause_btn)
        tb_l.addWidget(self.task_resume_btn)
        tb_l.addWidget(self.task_delete_btn)
        tb_l.addWidget(self.task_next_btn)

        task_box = QGroupBox("Task State")
        task_box.setMinimumWidth(320)
        task_layout = QVBoxLayout(task_box)
        task_layout.setContentsMargins(8, 8, 8, 8)
        task_layout.setSpacing(6)
        task_layout.addWidget(self.task_state_box)
        task_layout.addWidget(task_buttons_row)

        # ===== top params =====
        self.model_selector = QComboBox()
        self.model_selector.setFixedWidth(260)
        self.model_selector.addItems(["gpt-3.5-turbo", "gpt-4o-mini", "gpt-4o", "gpt-5.2-chat-latest"])
        self.model_selector.currentTextChanged.connect(self.on_model_changed)

        self.endpoint_selector = QComboBox()
        self.endpoint_selector.setFixedWidth(190)
        self.endpoint_selector.addItem("Chat Completions", "chat")
        self.endpoint_selector.addItem("Responses", "responses")

        self.temperature_input = QDoubleSpinBox()
        self.temperature_input.setFixedWidth(120)
        self.temperature_input.setDecimals(1)
        self.temperature_input.setSingleStep(0.1)
        self.temperature_input.setRange(0.0, 2.0)
        self.temperature_input.setValue(1.0)

        self.max_tokens_input = QSpinBox()
        self.max_tokens_input.setRange(1, 32000)
        self.max_tokens_input.setValue(800)
        self.max_tokens_input.setFixedWidth(120)

        self.keep_last_n_input = QSpinBox()
        self.keep_last_n_input.setRange(1, 100)
        self.keep_last_n_input.setValue(10)
        self.keep_last_n_input.setFixedWidth(120)

        self.strategy_selector = QComboBox()
        self.strategy_selector.setFixedWidth(240)
        self.strategy_selector.addItem("Sliding Window (последние N)", "sliding")
        self.strategy_selector.addItem("Sticky Facts + последние N", "facts")
        self.strategy_selector.addItem("Summary + последние N", "summary")
        self.strategy_selector.addItem("Branching (ветки) + последние N", "branching")
        self.strategy_selector.currentIndexChanged.connect(self.on_strategy_changed)
        self.summary_model_selector = QComboBox()
        self.summary_model_selector.setFixedWidth(170)
        self.summary_model_selector.addItems(["gpt-3.5-turbo", "gpt-4o-mini", "gpt-4o", "gpt-5.2-chat-latest"])

        self.summary_endpoint_selector = QComboBox()
        self.summary_endpoint_selector.setFixedWidth(130)
        self.summary_endpoint_selector.addItem("Chat", "chat")
        self.summary_endpoint_selector.addItem("Responses", "responses")

        self.summary_temperature_input = QDoubleSpinBox()
        self.summary_temperature_input.setFixedWidth(90)
        self.summary_temperature_input.setDecimals(1)
        self.summary_temperature_input.setSingleStep(0.1)
        self.summary_temperature_input.setRange(0.0, 2.0)
        self.summary_temperature_input.setValue(1.0)

        self.summary_max_tokens_input = QSpinBox()
        self.summary_max_tokens_input.setFixedWidth(90)
        self.summary_max_tokens_input.setRange(32, 8000)
        self.summary_max_tokens_input.setValue(600)

        # branching controls
        self.branch_selector = QComboBox()
        self.branch_selector.setFixedWidth(240)
        self.branch_selector.currentIndexChanged.connect(self.on_branch_changed)

        self.checkpoint_name = QLineEdit()
        self.checkpoint_name.setPlaceholderText("Имя checkpoint (опционально)")
        self.checkpoint_name.setFixedWidth(240)

        self.create_checkpoint_btn = QPushButton("Создать checkpoint")
        self.create_checkpoint_btn.clicked.connect(self.on_create_checkpoint_clicked)

        self.checkpoint_selector = QComboBox()
        self.checkpoint_selector.setFixedWidth(240)

        self.new_branch_name = QLineEdit()
        self.new_branch_name.setPlaceholderText("Имя новой ветки (опц.)")
        self.new_branch_name.setFixedWidth(240)

        self.create_branch_btn = QPushButton("Создать ветку от checkpoint")
        self.create_branch_btn.clicked.connect(self.on_create_branch_clicked)

        # ===== chat widgets =====
        self.input_editbox = QTextEdit()
        self.input_editbox.setFont(font)
        self.input_editbox.setPlaceholderText("Напиши сообщение. Shift+Enter — новая строка, Enter — отправить.")
        self.input_editbox.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        set_editbox_height(self.input_editbox, 6)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setVisible(False)
        self.sent_len_label = QLabel("API context tokens(est): N/A")
        self.task_signal_label = QLabel("Task signal: idle")

        self.output_editbox = QTextEdit()
        self.output_editbox.setFont(font)
        self.output_editbox.setReadOnly(True)

        self.stop_button = QPushButton("STOP")
        self.stop_button.clicked.connect(self.stop_generation)
        self.stop_button.setEnabled(False)

        self.clear_output_button = QPushButton("CLEAR")
        self.clear_output_button.clicked.connect(self.on_clear_output_clicked)

        btn_row = QWidget()
        btn_l = QHBoxLayout(btn_row)
        btn_l.setContentsMargins(0, 0, 0, 0)
        btn_l.setSpacing(8)
        btn_l.addWidget(self.stop_button)
        btn_l.addStretch(1)
        btn_l.addWidget(self.clear_output_button)

        # ===== right panel (params + branching + metrics + facts) =====
        right_panel = QWidget()
        rp = QVBoxLayout(right_panel)
        rp.setContentsMargins(0, 0, 0, 0)
        rp.setSpacing(10)

        params_box = QGroupBox("Параметры запроса")
        p_l = QVBoxLayout(params_box)
        p_l.setContentsMargins(8, 8, 8, 8)
        p_l.setSpacing(6)

        row1 = QWidget()
        r1 = QHBoxLayout(row1)
        r1.setContentsMargins(0, 0, 0, 0)
        r1.setSpacing(8)
        r1.addWidget(QLabel("Модель:"))
        r1.addStretch(1)
        r1.addWidget(self.model_selector)

        row2 = QWidget()
        r2 = QHBoxLayout(row2)
        r2.setContentsMargins(0, 0, 0, 0)
        r2.setSpacing(8)
        r2.addWidget(QLabel("Эндпоинт:"))
        r2.addStretch(1)
        r2.addWidget(self.endpoint_selector)

        row3 = QWidget()
        r3 = QHBoxLayout(row3)
        r3.setContentsMargins(0, 0, 0, 0)
        r3.setSpacing(8)
        r3.addWidget(QLabel("temperature:"))
        r3.addStretch(1)
        r3.addWidget(self.temperature_input)

        row4 = QWidget()
        r4 = QHBoxLayout(row4)
        r4.setContentsMargins(0, 0, 0, 0)
        r4.setSpacing(8)
        r4.addWidget(QLabel("max_tokens:"))
        r4.addStretch(1)
        r4.addWidget(self.max_tokens_input)

        row5 = QWidget()
        r5 = QHBoxLayout(row5)
        r5.setContentsMargins(0, 0, 0, 0)
        r5.setSpacing(8)
        r5.addWidget(QLabel("N сообщений (user+assistant):"))
        r5.addStretch(1)
        r5.addWidget(self.keep_last_n_input)

        row6 = QWidget()
        r6 = QHBoxLayout(row6)
        r6.setContentsMargins(0, 0, 0, 0)
        r6.setSpacing(8)
        r6.addWidget(QLabel("Стратегия контекста:"))
        r6.addStretch(1)
        r6.addWidget(self.strategy_selector)

        p_l.addWidget(row1)
        p_l.addWidget(row2)
        p_l.addWidget(row3)
        p_l.addWidget(row4)
        p_l.addWidget(row5)
        p_l.addWidget(row6)

        self.summary_params_row = QWidget()
        spr = QHBoxLayout(self.summary_params_row)
        spr.setContentsMargins(0, 0, 0, 0)
        spr.setSpacing(8)
        spr.addWidget(QLabel("Summary model:"))
        spr.addWidget(self.summary_model_selector)
        spr.addWidget(QLabel("Endpoint:"))
        spr.addWidget(self.summary_endpoint_selector)
        spr.addWidget(QLabel("Temp:"))
        spr.addWidget(self.summary_temperature_input)
        spr.addWidget(QLabel("Tokens:"))
        spr.addWidget(self.summary_max_tokens_input)
        spr.addStretch(1)
        p_l.addWidget(self.summary_params_row)
        self.summary_params_row.setVisible(False)

        branch_box = QGroupBox("Branching (ветки диалога)")
        b_l = QVBoxLayout(branch_box)
        b_l.setContentsMargins(8, 8, 8, 8)
        b_l.setSpacing(6)

        b_row1 = QWidget()
        b1 = QHBoxLayout(b_row1)
        b1.setContentsMargins(0, 0, 0, 0)
        b1.setSpacing(8)
        b1.addWidget(QLabel("Текущая ветка:"))
        b1.addStretch(1)
        b1.addWidget(self.branch_selector)

        b_row2 = QWidget()
        b2 = QHBoxLayout(b_row2)
        b2.setContentsMargins(0, 0, 0, 0)
        b2.setSpacing(8)
        b2.addWidget(QLabel("Checkpoint:"))
        b2.addStretch(1)
        b2.addWidget(self.checkpoint_name)

        b_row3 = QWidget()
        b3 = QHBoxLayout(b_row3)
        b3.setContentsMargins(0, 0, 0, 0)
        b3.setSpacing(8)
        b3.addStretch(1)
        b3.addWidget(self.create_checkpoint_btn)

        b_row4 = QWidget()
        b4 = QHBoxLayout(b_row4)
        b4.setContentsMargins(0, 0, 0, 0)
        b4.setSpacing(8)
        b4.addWidget(QLabel("Выбрать checkpoint:"))
        b4.addStretch(1)
        b4.addWidget(self.checkpoint_selector)

        b_row5 = QWidget()
        b5 = QHBoxLayout(b_row5)
        b5.setContentsMargins(0, 0, 0, 0)
        b5.setSpacing(8)
        b5.addWidget(QLabel("Новая ветка:"))
        b5.addStretch(1)
        b5.addWidget(self.new_branch_name)

        b_row6 = QWidget()
        b6 = QHBoxLayout(b_row6)
        b6.setContentsMargins(0, 0, 0, 0)
        b6.setSpacing(8)
        b6.addStretch(1)
        b6.addWidget(self.create_branch_btn)

        b_l.addWidget(b_row1)
        b_l.addWidget(b_row2)
        b_l.addWidget(b_row3)
        b_l.addWidget(b_row4)
        b_l.addWidget(b_row5)
        b_l.addWidget(b_row6)

        self.facts_box = QTextEdit()
        self.facts_box.setReadOnly(True)
        self.facts_box.setMinimumHeight(90)
        self.facts_box.setPlaceholderText("FACTS для стратегии Sticky Facts (показывается, когда она активна).")

        self.facts_group = QGroupBox("Facts:")
        fg = QVBoxLayout(self.facts_group)
        fg.setContentsMargins(8, 8, 8, 8)
        fg.addWidget(self.facts_box)

        rp.addWidget(params_box)
        rp.addWidget(branch_box)
        rp.addWidget(self.facts_group)
        rp.addStretch(1)

        # ===== main splitter =====
        left_panel = QWidget()
        lp = QVBoxLayout(left_panel)
        # ВАЖНО: даём правый отступ, чтобы левый контент не прилипал к сплиттеру
        lp.setContentsMargins(8, 8, 12, 8)
        lp.setSpacing(10)
        top_left_row = QWidget()
        tl = QHBoxLayout(top_left_row)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(8)
        tl.addWidget(session_box)
        tl.addWidget(task_box, 1)
        lp.addWidget(top_left_row)
        lp.addWidget(QLabel("Ввод:"))
        lp.addWidget(self.input_editbox)
        lp.addWidget(self.sent_len_label)
        lp.addWidget(self.task_signal_label)
        lp.addWidget(self.progress_bar)
        lp.addWidget(QLabel("Вывод:"))
        lp.addWidget(self.output_editbox)
        lp.addWidget(btn_row)

        self.vertical_splitter = QSplitter(Qt.Horizontal)
        self.vertical_splitter.addWidget(left_panel)
        self.vertical_splitter.addWidget(right_panel)
        self.vertical_splitter.setStretchFactor(0, 3)
        self.vertical_splitter.setStretchFactor(1, 2)

        tab_layout = QVBoxLayout(self.top_widget)
        tab_layout.setContentsMargins(8, 8, 8, 8)
        tab_layout.addWidget(self.vertical_splitter)

        self.on_model_changed(self.model_selector.currentText())
        self.on_strategy_changed()
        self.refresh_sessions_timer = QTimer(self)
        # TODO: Рассмотреть увеличение интервала/переход на event-driven обновление списка сессий (минимизация polling).
        self.refresh_sessions_timer.setInterval(1000)
        self.refresh_sessions_timer.timeout.connect(self._tick_refresh_sessions_list)
        self.refresh_sessions_timer.start()

        self.output_editbox.setMinimumHeight(160)
        self.render_task_state({})

    # Обновляет внутреннее состояние объекта и синхронизирует связанные элементы интерфейса или данные.

    def set_loading(self, is_loading: bool):
        if is_loading:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)
            if not self.is_generating:
                self.task_signal_label.setText("Task signal: idle")
        self.render_task_state(self.task_state_cache)

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def eventFilter(self, obj, event):
        if obj is self.input_editbox and event.type() == QEvent.KeyPress:
            key = event.key()
            mods = event.modifiers()

            if key in (Qt.Key_Return, Qt.Key_Enter):
                if mods & Qt.ShiftModifier:
                    return False
                self.on_send_message()
                return True
        return super().eventFilter(obj, event)

    # === Сохранение layout вкладки ===

    # Отслеживает изменения сплиттеров и сохраняет/восстанавливает геометрию панели чата между запусками приложения.

    # Реакция на пользовательское событие UI: валидирует текущее состояние и запускает соответствующую бизнес-операцию.

    def on_splitter_moved(self):
        self.splitter_move_timer.start(300)

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def save_window_state(self):
        try:
            state = {
                "log_splitter": self.log_splitter.saveState().toHex().data().decode(),
                "vertical_splitter": self.vertical_splitter.saveState().toHex().data().decode(),
            }
            with open(self.CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.logger.error_handler(e, context="ChatTab -> save_window_state")

    # Загружает данные из источника, нормализует формат и возвращает объект, пригодный для дальнейшей обработки.

    def load_window_state(self):
        if not os.path.exists(self.CONFIG_FILE):
            return
        try:
            with open(self.CONFIG_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)

            if "log_splitter" in state:
                self.log_splitter.restoreState(QByteArray.fromHex(str(state["log_splitter"]).encode()))
            if "vertical_splitter" in state:
                self.vertical_splitter.restoreState(QByteArray.fromHex(str(state["vertical_splitter"]).encode()))
        except Exception as e:
            self.logger.error(f"Ошибка загрузки состояния окна для вкладки ChatTab: {e}")

    # === Бутстрап соединения и профилей ===

    # Проверяет доступность агента, поднимает начальные данные профилей и синхронизирует элементы профилей в интерфейсе.

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    async def preload_agent_status(self):
        try:
            self.logger.info("Подключение к агенту (локальный сервер)...")
            ok = await self.agent.ping()
            self.is_agent_connected = bool(ok)
            if self.is_agent_connected:
                self.logger.success("Агент найден: подключение успешно")
                await self.refresh_sessions_list()
                await self.load_session_to_ui(self.current_session_id)
                await self.bootstrap_profile_ui()
                await self.refresh_task_state()
            else:
                self.logger.warning("Агент не отвечает. Запусти core/agent/agent_server.py перед UI.")
                self._set_profile_none_ui()
                self.render_task_state({})
        except Exception as e:
            self.is_agent_connected = False
            self.logger.warning(f"Не удалось подключиться к агенту: {e}")
            self._set_profile_none_ui()
            self.render_task_state({})

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    async def bootstrap_profile_ui(self):
        if not self.is_agent_connected:
            self._set_profile_none_ui()
            return
        try:
            # По ТЗ на старте UI профиль должен быть не выбран.
            await self.agent.set_active_profile("")
            state = await self.agent.get_profile_state()
            profiles = state.get("profiles") if isinstance(state.get("profiles"), list) else []
            self._populate_profile_selector(profiles=profiles, active_profile="")
            self.profile_description_input.clear()
            self.profile_use_toggle.blockSignals(True)
            self.profile_use_toggle.setChecked(False)
            self.profile_use_toggle.blockSignals(False)
            self.profile_use_toggle.setEnabled(False)
            self.active_profile_name = ""
        except Exception as e:
            self.logger.warning(f"Не удалось инициализировать профили: {e}")
            self._set_profile_none_ui()

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def _set_profile_none_ui(self):
        self._profile_selector_changing = True
        try:
            self.profile_selector.clear()
            self.profile_selector.addItem(self.profile_none_label, "")
            self.profile_selector.setCurrentIndex(0)
            self.profile_description_input.clear()
            self.profile_use_toggle.blockSignals(True)
            self.profile_use_toggle.setChecked(False)
            self.profile_use_toggle.blockSignals(False)
            self.profile_use_toggle.setEnabled(False)
            self.active_profile_name = ""
        finally:
            self._profile_selector_changing = False

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def _populate_profile_selector(self, profiles: list[str], active_profile: str = ""):
        clean_active = str(active_profile or "").strip()
        self._profile_selector_changing = True
        try:
            self.profile_selector.clear()
            self.profile_selector.addItem(self.profile_none_label, "")
            for name in profiles:
                clean_name = str(name or "").strip()
                if clean_name:
                    self.profile_selector.addItem(clean_name, clean_name)
            if clean_active:
                idx = self.profile_selector.findData(clean_active)
                if idx >= 0:
                    self.profile_selector.setCurrentIndex(idx)
                else:
                    self.profile_selector.setCurrentIndex(0)
                    self.profile_selector.setEditText(clean_active)
            else:
                self.profile_selector.setCurrentIndex(0)
        finally:
            self._profile_selector_changing = False

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def _selected_profile_name(self) -> str:
        text = str(self.profile_selector.currentText() or "").strip()
        if not text or text == self.profile_none_label:
            return ""
        return text

    # Реакция на пользовательское событие UI: валидирует текущее состояние и запускает соответствующую бизнес-операцию.

    def on_profile_selected(self, _index=None):
        if self._profile_selector_changing:
            return
        asyncio.get_event_loop().create_task(self._on_profile_selected_async())

    # Реакция на пользовательское событие UI: валидирует текущее состояние и запускает соответствующую бизнес-операцию.

    def on_profile_text_changed(self, _text: str):
        if self._profile_selector_changing:
            return
        selected = self._selected_profile_name()
        if (not selected) or (selected != self.active_profile_name):
            self.profile_use_toggle.blockSignals(True)
            self.profile_use_toggle.setChecked(False)
            self.profile_use_toggle.blockSignals(False)
            self.profile_use_toggle.setEnabled(False)

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    async def _on_profile_selected_async(self):
        selected = self._selected_profile_name()
        if not self.is_agent_connected:
            if not selected:
                self.profile_use_toggle.blockSignals(True)
                self.profile_use_toggle.setChecked(False)
                self.profile_use_toggle.blockSignals(False)
                self.profile_use_toggle.setEnabled(False)
            return
        try:
            if not selected:
                await self.agent.set_active_profile("")
                self.profile_description_input.clear()
                self.profile_use_toggle.blockSignals(True)
                self.profile_use_toggle.setChecked(False)
                self.profile_use_toggle.blockSignals(False)
                self.profile_use_toggle.setEnabled(False)
                self.active_profile_name = ""
                self.logger.info("Активный профиль: Без профиля")
                return

            await self.agent.set_active_profile(selected)
            profile = await self.agent.get_profile(selected)
            description = ""
            if isinstance(profile, dict):
                description = str(profile.get("description") or "")
            self.profile_description_input.setPlainText(description)
            self.profile_use_toggle.setEnabled(True)
            self.active_profile_name = selected
            self.logger.success(f"Активный профиль: {selected}")
        except Exception as e:
            self.logger.warning(f"Не удалось выбрать профиль: {e}")

    # Реакция на пользовательское событие UI: валидирует текущее состояние и запускает соответствующую бизнес-операцию.

    def on_save_profile_clicked(self, _checked=False):
        asyncio.get_event_loop().create_task(self._save_profile_async())

    # Сохраняет или фиксирует данные в целевом хранилище с базовой валидацией входных параметров.

    async def _save_profile_async(self):
        if not self.is_agent_connected:
            self.logger.warning("Агент OFFLINE: сохранение профиля недоступно.")
            return
        profile_name = self._selected_profile_name()
        if not profile_name:
            self.logger.warning("Укажите имя профиля в выпадающем списке.")
            return
        description = self.profile_description_input.toPlainText().strip()
        try:
            payload = await self.agent.save_profile(profile_name, description)
            profiles = payload.get("profiles") if isinstance(payload.get("profiles"), list) else []
            active = str(payload.get("active_profile") or profile_name)
            self._populate_profile_selector(profiles=profiles, active_profile=active)
            idx = self.profile_selector.findData(active)
            if idx >= 0:
                self.profile_selector.setCurrentIndex(idx)
            self.profile_use_toggle.setEnabled(True)
            self.active_profile_name = active
            self.logger.success(f"Профиль сохранен: {active}")
        except Exception as e:
            self.logger.warning(f"Не удалось сохранить профиль: {e}")

    # Реакция на пользовательское событие UI: валидирует текущее состояние и запускает соответствующую бизнес-операцию.

    def on_delete_profile_clicked(self, _checked=False):
        asyncio.get_event_loop().create_task(self._delete_profile_async())

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    async def _delete_profile_async(self):
        if not self.is_agent_connected:
            self.logger.warning("Агент OFFLINE: удаление профиля недоступно.")
            return
        profile_name = self._selected_profile_name()
        if not profile_name:
            self.logger.warning("Сначала выберите профиль для удаления.")
            return
        try:
            payload = await self.agent.delete_profile(profile_name)
            profiles = payload.get("profiles") if isinstance(payload.get("profiles"), list) else []
            await self.agent.set_active_profile("")
            self._populate_profile_selector(profiles=profiles, active_profile="")
            self.profile_description_input.clear()
            self.profile_use_toggle.blockSignals(True)
            self.profile_use_toggle.setChecked(False)
            self.profile_use_toggle.blockSignals(False)
            self.profile_use_toggle.setEnabled(False)
            self.active_profile_name = ""
            self.logger.success(f"Профиль удален: {profile_name}")
        except Exception as e:
            self.logger.warning(f"Не удалось удалить профиль: {e}")

    # Реакция на пользовательское событие UI: валидирует текущее состояние и запускает соответствующую бизнес-операцию.

    def on_profile_toggle_changed(self, checked: bool):
        if not checked:
            return
        if not self._selected_profile_name():
            self.profile_use_toggle.blockSignals(True)
            self.profile_use_toggle.setChecked(False)
            self.profile_use_toggle.blockSignals(False)
            self.profile_use_toggle.setEnabled(False)
            self.logger.warning("Нельзя включить персонализацию без выбранного профиля.")

    # === Task state ===

    def render_task_state(self, task_state: dict):
        if not isinstance(task_state, dict):
            task_state = {}
        normalized = {
            "task": str(task_state.get("task") or "").strip(),
            "state": str(task_state.get("state") or "planning").strip().lower(),
            "step": int(task_state.get("step") or 0),
            "total": int(task_state.get("total") or 0),
            "current": str(task_state.get("current") or "").strip(),
            "expected_action": str(task_state.get("expected_action") or "").strip(),
            "is_paused": bool(task_state.get("is_paused", False)),
            "plan": task_state.get("plan") if isinstance(task_state.get("plan"), list) else [],
            "done": task_state.get("done") if isinstance(task_state.get("done"), list) else [],
        }
        self.task_state_cache = normalized
        signature = json.dumps(normalized, ensure_ascii=False, sort_keys=True)

        task = normalized["task"]
        state = normalized["state"]
        step = normalized["step"]
        total = normalized["total"]
        current = normalized["current"]
        expected_action = normalized["expected_action"]
        paused = normalized["is_paused"]
        plan = normalized["plan"]
        done = normalized["done"]

        if signature != self._task_state_signature:
            scrollbar = self.task_state_box.verticalScrollBar()
            prev_value = int(scrollbar.value()) if scrollbar is not None else 0

            if not task and not plan:
                self.task_state_box.setPlainText("Задача пока не инициализирована.")
            else:
                lines = [
                    f"Task: {task or '-'}",
                    f"State: {state}",
                    f"Step: {step}/{total}",
                    f"Paused: {paused}",
                    f"Current: {current or '-'}",
                    f"Expected action: {expected_action or '-'}",
                    "",
                    "[PLAN]",
                ]
                if plan:
                    for idx, item in enumerate(plan, start=1):
                        marker = "->" if idx == step and state != "done" else "  "
                        lines.append(f"{marker} {idx}. {item}")
                else:
                    lines.append("- (empty)")
                lines.append("")
                lines.append("[DONE]")
                if done:
                    for item in done:
                        lines.append(f"- {item}")
                else:
                    lines.append("- (empty)")
                self.task_state_box.setPlainText("\n".join(lines))

            if scrollbar is not None:
                scrollbar.setValue(min(prev_value, int(scrollbar.maximum())))
            self._task_state_signature = signature

        has_task = bool(task) and total > 0
        can_pause = has_task and (not paused)
        can_resume = has_task and paused

        self.task_confirm_btn.setEnabled(False)
        self.task_pause_btn.setEnabled(can_pause and not self.is_generating)
        self.task_resume_btn.setEnabled(can_resume and not self.is_generating)
        self.task_delete_btn.setEnabled((has_task or bool(task) or bool(plan)) and not self.is_generating)
        self.task_next_btn.setEnabled(False)

    async def refresh_task_state(self):
        if not self.is_agent_connected:
            self.render_task_state({})
            return
        try:
            task_state = await self.agent.get_task_state()
            self.render_task_state(task_state)
        except Exception as e:
            self.logger.warning(f"Не удалось получить task_state: {e}")

    def on_task_confirm_clicked(self):
        asyncio.get_event_loop().create_task(self._confirm_task_async())

    async def _confirm_task_async(self):
        if not self.is_agent_connected:
            self.logger.warning("Агент OFFLINE: confirm plan недоступен.")
            return
        try:
            task_state = await self.agent.confirm_task_plan()
            self.render_task_state(task_state)
            self.logger.success("План подтвержден. Состояние: execution.")
        except Exception as e:
            self.logger.warning(f"Не удалось подтвердить план: {e}")

    def on_task_pause_clicked(self):
        asyncio.get_event_loop().create_task(self._pause_task_async())

    async def _pause_task_async(self):
        if not self.is_agent_connected:
            self.logger.warning("Агент OFFLINE: pause недоступен.")
            return
        try:
            task_state = await self.agent.pause_task()
            self.render_task_state(task_state)
            self.logger.success("Задача поставлена на паузу.")
        except Exception as e:
            self.logger.warning(f"Не удалось поставить задачу на паузу: {e}")

    def on_task_resume_clicked(self):
        asyncio.get_event_loop().create_task(self._resume_task_async())

    async def _resume_task_async(self):
        if not self.is_agent_connected:
            self.logger.warning("Агент OFFLINE: resume недоступен.")
            return
        try:
            task_state = await self.agent.resume_task()
            self.render_task_state(task_state)
            self.logger.success("Задача возвращена в контекст.")
        except Exception as e:
            self.logger.warning(f"Не удалось продолжить задачу: {e}")

    def on_task_next_clicked(self):
        asyncio.get_event_loop().create_task(self._next_task_async())

    async def _next_task_async(self):
        if not self.is_agent_connected:
            self.logger.warning("Агент OFFLINE: next step недоступен.")
            return
        try:
            task_state = await self.agent.next_task_step()
            self.render_task_state(task_state)
            self.logger.success("Переход к следующему шагу выполнен.")
        except Exception as e:
            self.logger.warning(f"Не удалось перейти к следующему шагу: {e}")

    def on_task_delete_clicked(self):
        asyncio.get_event_loop().create_task(self._delete_task_async())

    async def _delete_task_async(self):
        if not self.is_agent_connected:
            self.logger.warning("Агент OFFLINE: delete task недоступен.")
            return
        try:
            task_state = await self.agent.delete_task()
            self.render_task_state(task_state)
            self.task_signal_label.setText("Task signal: task deleted")
            self.logger.success("Активная задача удалена.")
        except Exception as e:
            self.logger.warning(f"Не удалось удалить задачу: {e}")

    def _on_task_signal(self, payload: dict):
        if not isinstance(payload, dict):
            return
        stage = str(payload.get("stage") or "").strip()
        message = str(payload.get("message") or "").strip()
        if not message and not stage:
            return
        if stage and message:
            self.task_signal_label.setText(f"Task signal [{stage}]: {message}")
        elif message:
            self.task_signal_label.setText(f"Task signal: {message}")
        else:
            self.task_signal_label.setText(f"Task signal [{stage}]")

    # === Список сессий и загрузка состояния ===

    # Показывает offline-список, периодически синхронизируется с сервером и загружает выбранную сессию вместе с ветками/checkpoints.

    # Запрашивает актуальные данные и обновляет текущий экран/состояние, сохраняя консистентность активного контекста.

    async def refresh_sessions_list_offline_first(self):
        # сразу покажем текущую сессию (даже если агент оффлайн)
        self.render_sessions_list_offline()
        await asyncio.sleep(0.1)
        if self.is_agent_connected:
            await self.refresh_sessions_list()

    # Периодический таймерный обработчик: запускает лёгкую синхронизацию и не блокирует основной поток интерфейса.

    def _tick_refresh_sessions_list(self):
        """
        Таймер раз в N мс обновляет список сессий.
        Метод НЕ async, поэтому создаём задачу через event loop.
        Защита от параллельных запусков, чтобы не спамить запросами к агенту.
        """
        if not self.is_agent_connected:
            return

        if getattr(self, "_sessions_refresh_inflight", False):
            return

        self._sessions_refresh_inflight = True

        async def _run():
            try:
                await self.refresh_sessions_list()
                await self.refresh_task_state()
            finally:
                self._sessions_refresh_inflight = False

        try:
            asyncio.get_event_loop().create_task(_run())
        except Exception:
            # если по какой-то причине нет event loop — просто снимаем флаг
            self._sessions_refresh_inflight = False

    # Преобразует входные данные в представление для интерфейса и обновляет видимые панели без изменения бизнес-данных.

    def render_sessions_list_offline(self):
        self.sessions_list.blockSignals(True)
        self.sessions_list.clear()
        item = QListWidgetItem(f"{self.current_session_id} — (текущая, новая)")
        item.setData(Qt.UserRole, self.current_session_id)
        self.sessions_list.addItem(item)
        self.sessions_list.blockSignals(False)

    # Запрашивает актуальные данные и обновляет текущий экран/состояние, сохраняя консистентность активного контекста.

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

        found_current = False
        for s in sessions:
            sid = (s.get("session_id") or "").strip()
            title = (s.get("title") or "").strip()
            if not sid:
                continue
            if sid == self.current_session_id:
                found_current = True

            item = QListWidgetItem(f"{sid} — {title or 'Без темы'}")
            item.setData(Qt.UserRole, sid)
            self.sessions_list.addItem(item)

        if not found_current:
            item = QListWidgetItem(f"{self.current_session_id} — (текущая, новая)")
            item.setData(Qt.UserRole, self.current_session_id)
            self.sessions_list.insertItem(0, item)

        self.sessions_list.blockSignals(False)

    # Реакция на пользовательское событие UI: валидирует текущее состояние и запускает соответствующую бизнес-операцию.

    def on_session_clicked(self, item: QListWidgetItem):
        sid = item.data(Qt.UserRole)
        if not sid or self.is_generating:
            return
        self.current_session_id = str(sid)
        self._clear_session_dependent_ui(clear_input=True)
        asyncio.get_event_loop().create_task(self.load_session_to_ui(self.current_session_id))

    # Загружает сессию из агента и синхронизирует состояние вкладки: вывод, активную ветку, checkpoints, facts и memory-панели.

    async def load_session_to_ui(self, session_id: str):
        if not self.is_agent_connected:
            self.logger.warning("Агент OFFLINE: не могу загрузить историю")
            self.render_sessions_list_offline()
            return

        try:
            session = await self.agent.get_session(session_id)
        except Exception as e:
            self.logger.warning(f"Не удалось загрузить сессию {session_id}: {e}")
            return

        if not session:
            return

        self._clear_session_dependent_ui()

        branches = session.get("branches") or {}
        if not isinstance(branches, dict):
            branches = {}

        strategy = self.strategy_selector.currentData()
        is_branching = (strategy == "branching")
        active_branch = (session.get("active_branch") or "main").strip() or "main"
        if is_branching:
            self.current_branch_id = active_branch
        else:
            self.current_branch_id = "main"

        # fill branch selector
        self.branch_selector.blockSignals(True)
        self.branch_selector.clear()
        if is_branching:
            for bid, b in branches.items():
                name = (b.get("name") or b.get("title") or bid).strip()
                self.branch_selector.addItem(f"{name} ({bid})", bid)
        else:
            self.branch_selector.addItem("main (main)", "main")
        idx = self.branch_selector.findData(self.current_branch_id)
        if idx >= 0:
            self.branch_selector.setCurrentIndex(idx)
        self.branch_selector.blockSignals(False)

        # fill checkpoints
        self.refresh_checkpoints_ui(branches.get(self.current_branch_id) or {})

        # render history of active branch
        branch = branches.get(self.current_branch_id) or {}
        history = branch.get("history") or []
        for m in history:
            role = (m.get("role") or "").strip()
            content = m.get("content") or ""
            if role == "user":
                self.output_editbox.append(f"Ты: {content}\n")
            elif role == "assistant":
                self.output_editbox.append(f"GPT: {content}\n")
            else:
                self.output_editbox.append(f"{role}: {content}\n")

        # show facts only for Sticky Facts strategy
        facts = branch.get("facts") if isinstance(branch.get("facts"), dict) else {}
        if strategy == "facts":
            self.render_facts(facts)
        else:
            self.facts_box.clear()
        memory_layers = branch.get("memory_layers") if isinstance(branch.get("memory_layers"), dict) else {}
        self.render_memory_layers(memory_layers)

        self.logger.debug(f"Сессия загружена. session={session_id} branch={self.current_branch_id}")

    # Запрашивает актуальные данные и обновляет текущий экран/состояние, сохраняя консистентность активного контекста.

    def refresh_checkpoints_ui(self, branch: dict):
        """
        Сервер хранит checkpoints как list[dict], но на всякий случай поддержим и старый формат dict.
        В combo кладём data = checkpoint_id, а текст делаем читаемым.
        """
        cps_raw = branch.get("checkpoints")

        checkpoints = []

        if isinstance(cps_raw, list):
            for cp in cps_raw:
                if isinstance(cp, dict) and cp.get("id"):
                    checkpoints.append(cp)

        elif isinstance(cps_raw, dict):
            # старый формат (на всякий случай)
            for cp_id, cp_val in cps_raw.items():
                if isinstance(cp_val, dict):
                    cp = dict(cp_val)
                    cp["id"] = cp.get("id") or cp_id
                    checkpoints.append(cp)
                else:
                    checkpoints.append({"id": str(cp_id), "name": str(cp_id)})

        self.checkpoint_selector.clear()

        for cp in checkpoints:
            cp_id = str(cp.get("id"))
            cp_name = (cp.get("name") or "").strip()
            if cp_name and cp_name != cp_id:
                label = f"{cp_name} ({cp_id})"
            else:
                label = cp_id
            self.checkpoint_selector.addItem(label, cp_id)

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def _clear_session_dependent_ui(self, clear_input: bool = False):
        self.output_editbox.clear()
        self.facts_box.clear()
        self.metrics_memory_tab.clear_panels()
        self.sent_len_label.setText("API context tokens(est): N/A")
        self.branch_selector.blockSignals(True)
        self.branch_selector.clear()
        self.branch_selector.blockSignals(False)
        self.checkpoint_selector.clear()
        self.checkpoint_name.clear()
        self.new_branch_name.clear()
        if clear_input:
            self.input_editbox.clear()

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def _reset_branch_ui_to_main(self):
        self.current_branch_id = "main"
        self.branch_selector.blockSignals(True)
        self.branch_selector.clear()
        self.branch_selector.addItem("main (main)", "main")
        self.branch_selector.setCurrentIndex(0)
        self.branch_selector.blockSignals(False)
        self.checkpoint_selector.clear()

    # === Отрисовка контекста памяти ===

    # Обновляет панели facts и memory layers по данным последнего ответа, чтобы пользователь видел текущий контекст агента.

    # Преобразует входные данные в представление для интерфейса и обновляет видимые панели без изменения бизнес-данных.

    def render_facts(self, facts: dict):
        if not isinstance(facts, dict) or not facts:
            self.facts_box.setPlainText("")
            return
        lines = [f"{k}: {v}" for k, v in facts.items()]
        self.facts_box.setPlainText("\n".join(lines))

    # Преобразует входные данные в представление для интерфейса и обновляет видимые панели без изменения бизнес-данных.

    def render_memory_layers(self, memory_layers: dict):
        if not isinstance(memory_layers, dict) or not memory_layers:
            self.memory_box.setPlainText("")
            return
        lines = []
        short_term = memory_layers.get("short_term")
        working = memory_layers.get("working")
        long_term = memory_layers.get("long_term")

        lines.append("[short_term]")
        if isinstance(short_term, list) and short_term:
            for item in short_term[-8:]:
                if isinstance(item, dict):
                    k = str(item.get("key") or "note")
                    v = str(item.get("value") or "")
                    lines.append(f"- {k}: {v}")
        else:
            lines.append("- (empty)")

        lines.append("")
        lines.append("[working]")
        if isinstance(working, dict) and working:
            for k, v in working.items():
                lines.append(f"- {k}: {v}")
        else:
            lines.append("- (empty)")

        lines.append("")
        lines.append("[long_term]")
        if isinstance(long_term, dict) and long_term:
            for k, v in long_term.items():
                lines.append(f"- {k}: {v}")
        else:
            lines.append("- (empty)")

        self.memory_box.setPlainText("\n".join(lines))

    # === Управление сессиями и стратегией ===

    # Создаёт/чистит сессии, переключает модель/стратегию и запускает серверные операции branching (switch/checkpoint/create branch).

    # Реакция на пользовательское событие UI: валидирует текущее состояние и запускает соответствующую бизнес-операцию.

    def on_new_session_clicked(self):
        if self.is_generating:
            self.logger.warning("Нельзя сменить сессию во время генерации.")
            return
        self.current_session_id = str(uuid.uuid4())
        self._clear_session_dependent_ui(clear_input=True)
        self._reset_branch_ui_to_main()
        self.render_sessions_list_offline()
        if self.is_agent_connected:
            asyncio.get_event_loop().create_task(self.refresh_sessions_list())
        self.logger.success(f"Создана новая сессия: {self.current_session_id}")

    # Реакция на пользовательское событие UI: валидирует текущее состояние и запускает соответствующую бизнес-операцию.

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
                    self._clear_session_dependent_ui(clear_input=True)
                    self.current_branch_id = "main"
                    await self.refresh_sessions_list()
                    await self.load_session_to_ui(self.current_session_id)
                    self.logger.success("История удалена.")
                else:
                    self.logger.warning("Не удалось удалить историю (agent вернул False).")
            except Exception as e:
                self.logger.warning(f"Ошибка удаления истории: {e}")

        asyncio.get_event_loop().create_task(_do())

    # Реакция на пользовательское событие UI: валидирует текущее состояние и запускает соответствующую бизнес-операцию.

    def on_model_changed(self, model_text: str):
        model_text = (model_text or "").strip()
        is_gpt52_locked = (model_text == "gpt-5.2-chat-latest")
        self.temperature_input.setEnabled(not is_gpt52_locked)
        if is_gpt52_locked:
            self.temperature_input.setValue(1.0)
            self.logger.warning("Для gpt-5.2-chat-latest temperature заблокирована ProxyAPI. Установлено 1.0.")

    # Реакция на пользовательское событие UI: валидирует текущее состояние и запускает соответствующую бизнес-операцию.

    def on_strategy_changed(self):
        strategy = self.strategy_selector.currentData()
        is_branching = (strategy == "branching")
        is_facts = (strategy == "facts")
        is_summary = (strategy == "summary")
        self.branch_selector.setEnabled(is_branching)
        self.checkpoint_name.setEnabled(is_branching)
        self.create_checkpoint_btn.setEnabled(is_branching)
        self.checkpoint_selector.setEnabled(is_branching)
        self.new_branch_name.setEnabled(is_branching)
        self.create_branch_btn.setEnabled(is_branching)
        self.facts_group.setEnabled(is_facts)
        self.facts_box.setEnabled(is_facts)
        self.summary_params_row.setVisible(is_summary)
        if not is_facts:
            self.facts_box.clear()

        if is_branching:
            self.logger.info("Стратегия: Branching. История ведётся по выбранной ветке.")
        elif strategy == "facts":
            self.logger.info("Стратегия: Sticky Facts. Facts обновляются после каждого сообщения пользователя.")
        elif strategy == "summary":
            self.logger.info("Стратегия: Summary. Старые сообщения сжимаются в summary + последние N отправляются как есть.")
        else:
            self.logger.info("Стратегия: Sliding Window. В модель отправляются только последние N сообщений.")

        if (not is_branching) and (self.current_branch_id != "main"):
            self.current_branch_id = "main"

        if self.is_agent_connected and (not self.is_generating):
            asyncio.get_event_loop().create_task(self.load_session_to_ui(self.current_session_id))

    # Реакция на пользовательское событие UI: валидирует текущее состояние и запускает соответствующую бизнес-операцию.

    def on_branch_changed(self):
        if self.strategy_selector.currentData() != "branching":
            return
        if self.is_generating:
            return
        bid = self.branch_selector.currentData()
        if not bid:
            return
        # переключение ветки происходит на сервере, чтобы сохранялось на диск
        asyncio.get_event_loop().create_task(self._switch_branch_async(str(bid)))

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    async def _switch_branch_async(self, branch_id: str):
        if not self.is_agent_connected:
            self.logger.warning("Агент OFFLINE: не могу сменить ветку.")
            return
        try:
            active = await self.agent.switch_branch(self.current_session_id, branch_id)
            self.current_branch_id = active
            await self.load_session_to_ui(self.current_session_id)
            self.logger.success(f"Активная ветка: {self.current_branch_id}")
        except Exception as e:
            self.logger.warning(f"Не удалось сменить ветку: {e}")

    # Реакция на пользовательское событие UI: валидирует текущее состояние и запускает соответствующую бизнес-операцию.

    def on_create_checkpoint_clicked(self):
        if self.strategy_selector.currentData() != "branching":
            self.logger.warning("Checkpoint доступен только в режиме Branching.")
            return
        if self.is_generating:
            return
        asyncio.get_event_loop().create_task(self._create_checkpoint_async())

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    async def _create_checkpoint_async(self):
        if not self.is_agent_connected:
            self.logger.warning("Агент OFFLINE: не могу создать checkpoint.")
            return
        name = self.checkpoint_name.text().strip()
        try:
            cp_id = await self.agent.create_checkpoint(self.current_session_id, self.current_branch_id, name=name)
            self.logger.success(f"Checkpoint создан: {cp_id}")
            self.checkpoint_name.clear()
            await self.load_session_to_ui(self.current_session_id)
        except Exception as e:
            self.logger.warning(f"Не удалось создать checkpoint: {e}")

    # Реакция на пользовательское событие UI: валидирует текущее состояние и запускает соответствующую бизнес-операцию.

    def on_create_branch_clicked(self):
        if self.strategy_selector.currentData() != "branching":
            self.logger.warning("Создание ветки доступно только в режиме Branching.")
            return
        if self.is_generating:
            return
        asyncio.get_event_loop().create_task(self._create_branch_async())

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    async def _create_branch_async(self):
        if not self.is_agent_connected:
            self.logger.warning("Агент OFFLINE: не могу создать ветку.")
            return
        cp_id = self.checkpoint_selector.currentData()
        if not cp_id:
            self.logger.warning("Сначала выбери checkpoint.")
            return
        name = self.new_branch_name.text().strip()
        try:
            new_bid = await self.agent.create_branch(self.current_session_id, self.current_branch_id, str(cp_id), new_branch_name=name)
            self.logger.success(f"Ветка создана: {new_bid}")
            self.new_branch_name.clear()
            await self.load_session_to_ui(self.current_session_id)
        except Exception as e:
            self.logger.warning(f"Не удалось создать ветку: {e}")

    # === Отправка сообщений и стриминг ===

    # Запускает запрос к агенту, стримит чанки ответа в UI, собирает метрики исполнения и корректно обрабатывает остановку генерации.

    # Реакция на пользовательское событие UI: валидирует текущее состояние и запускает соответствующую бизнес-операцию.

    def on_send_message(self):
        if self.is_generating:
            self.logger.warning("Модель ещё отвечает — подожди.")
            return

        text = self.input_editbox.toPlainText().strip()
        if not text:
            self.logger.warning("Отсутствует текст для отправки!")
            return

        self.input_editbox.clear()

        self.output_editbox.append(f"Ты: {text}\n")
        self.output_editbox.append("GPT: ")
        self.output_editbox.moveCursor(QTextCursor.End)
        self.output_editbox.ensureCursorVisible()

        self.set_loading(True)
        self.stop_requested = False
        self.is_generating = True
        self.stop_button.setEnabled(True)

        selected_model = self.model_selector.currentText().strip()
        selected_endpoint = self.endpoint_selector.currentData()
        selected_temperature = float(self.temperature_input.value()) if self.temperature_input.isEnabled() else None
        selected_max_tokens = int(self.max_tokens_input.value())
        keep_last_n = int(self.keep_last_n_input.value())
        strategy = self.strategy_selector.currentData()
        use_profile = bool(self.profile_use_toggle.isEnabled() and self.profile_use_toggle.isChecked())
        summary_config = None
        if strategy == "summary":
            summary_config = {
                "model": self.summary_model_selector.currentText().strip(),
                "endpoint": self.summary_endpoint_selector.currentData(),
                "temperature": float(self.summary_temperature_input.value()),
                "max_tokens": int(self.summary_max_tokens_input.value()),
            }

        self.current_task = asyncio.create_task(
            self.ask_and_stream_answer(
                user_text=text,
                model=selected_model,
                endpoint=selected_endpoint,
                temperature=selected_temperature,
                max_tokens=selected_max_tokens,
                keep_last_n=keep_last_n,
                strategy=strategy,
                memory_write=self.pending_memory_write,
                use_profile=use_profile,
                summary_config=summary_config,
            )
        )
        self.pending_memory_write = None

    # Ведёт полный цикл стриминга: проверка соединения, чтение чанков, расчёт TTFT/токенов/стоимости и обновление UI/метрик по итогам ответа.

    async def ask_and_stream_answer(
        self,
        user_text: str,
        model: str,
        endpoint: str,
        temperature,
        max_tokens: int,
        keep_last_n: int,
        strategy: str,
        memory_write=None,
        use_profile: bool = False,
        summary_config: dict | None = None,
    ):
        t0 = time.perf_counter()
        ttft_sec = None
        got_first = False
        gen = None
        error_text = None

        try:
            if not self.is_agent_connected:
                ok = await self.agent.ping()
                self.is_agent_connected = bool(ok)
                if not self.is_agent_connected:
                    self.output_editbox.append("\n[Ошибка] Агент не запущен или недоступен.\n")
                    return

            self.agent.on_task_signal = self._on_task_signal
            self.task_signal_label.setText("Task signal: запрос отправлен")
            gen = self.agent.stream_chat(
                user_text=user_text,
                model=model,
                endpoint=endpoint,
                max_tokens=max_tokens,
                temperature=temperature,
                session_id=self.current_session_id,
                branch_id=self.current_branch_id if strategy == "branching" else None,
                keep_last_n=keep_last_n,
                context_strategy=strategy,
                memory_write=memory_write,
                use_profile=use_profile,
                summary_config=summary_config,
            )

            async for chunk in gen:
                if self.stop_requested:
                    break
                if (not got_first) and chunk:
                    got_first = True
                    ttft_sec = time.perf_counter() - t0

                self.output_editbox.insertPlainText(chunk)
                self.output_editbox.moveCursor(QTextCursor.End)
                self.output_editbox.ensureCursorVisible()

            self.output_editbox.append("\n")

        except asyncio.CancelledError:
            self.output_editbox.append("\n[Остановлено пользователем]\n")
            raise
        except Exception as e:
            error_text = str(e)
            self.logger.error_handler(e, context="ChatTab -> ask_and_stream_answer")
            self.output_editbox.append(f"\n[Ошибка] {e}\n")

        finally:
            self.agent.on_task_signal = None
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
            cost_str = f"{cost_rub:.4f} ₽" if isinstance(cost_rub, (int, float)) else "N/A"
            ttft_str = f"{ttft_sec:.3f}s" if isinstance(ttft_sec, (int, float)) else "N/A"
            temp_str = f"{temperature}" if temperature is not None else "locked(1.0)"

            ms = getattr(self.agent, "last_message_stats", None) or {}
            token_stats = getattr(self.agent, "last_token_stats", None) or {}
            profile_info = getattr(self.agent, "last_profile_info", None) or {}
            task_state = getattr(self.agent, "last_task_state", None) or {}
            active_branch = getattr(self.agent, "last_active_branch", None) or self.current_branch_id
            self.current_branch_id = active_branch

            strategy_used = ms.get("strategy") or strategy
            facts_count = ms.get("facts_count")
            sent_messages = ms.get("sent_messages")
            api_context_tokens = token_stats.get("context_tokens_est")
            dialog_tokens = token_stats.get("dialog_tokens_est")
            user_tokens = token_stats.get("user_text_tokens_est")
            may_exceed = token_stats.get("may_exceed_context")
            use_profile_stat = bool(profile_info.get("use_profile", use_profile))
            active_profile_stat = str(profile_info.get("active_profile") or "Без профиля")
            desc_len_stat = int(profile_info.get("profile_description_len") or 0)
            task_state_stat = str(ms.get("task_state") or task_state.get("state") or "planning")
            task_step_stat = int(ms.get("task_step") or task_state.get("step") or 0)
            task_total_stat = int(ms.get("task_total") or task_state.get("total") or 0)
            task_paused_stat = bool(ms.get("task_paused", task_state.get("is_paused", False)))
            task_injected_stat = bool(ms.get("task_injected", False))
            self.sent_len_label.setText(f"API context tokens(est): {api_context_tokens if api_context_tokens is not None else 'N/A'}")

            line = (
                f"Strategy={strategy_used} | Branch={active_branch} | sent_msgs={sent_messages} | keep_last_n={keep_last_n} | "
                f"TTFT={ttft_str} | Total={total_sec:.3f}s | "
                f"prompt={prompt_tokens} | completion={completion_tokens} | total={total_tokens_call} | Cost={cost_str} | "
                f"Temp={temp_str} | max_tokens={max_tokens} | user_est={user_tokens} | ctx_est={api_context_tokens} | dialog_est={dialog_tokens} | overflow_risk={may_exceed} | "
                f"use_profile={use_profile_stat} | active_profile={active_profile_stat or 'Без профиля'} | description_len={desc_len_stat} | "
                f"task_state={task_state_stat} | task_step={task_step_stat}/{task_total_stat} | task_paused={task_paused_stat} | task_injected={task_injected_stat}"
            )
            if facts_count is not None:
                line += f" | facts={facts_count}"

            if error_text:
                short = error_text.replace("\n", " ")
                if len(short) > 160:
                    short = short[:160] + "..."
                line += f" | ERROR={short}"

            self.metrics_box.append(line)

            # facts panel update
            last_facts = getattr(self.agent, "last_facts", None)
            if strategy_used == "facts" and isinstance(last_facts, dict):
                self.render_facts(last_facts)
            else:
                self.facts_box.clear()
            last_memory = getattr(self.agent, "last_memory_layers", None)
            if isinstance(last_memory, dict):
                self.render_memory_layers(last_memory)
            if isinstance(task_state, dict):
                self.render_task_state(task_state)

            # refresh session list (title updates) — без перезагрузки UI (иначе может чистить поля)
            if self.is_agent_connected:
                asyncio.get_event_loop().create_task(self.refresh_sessions_list())

            self.is_generating = False
            self.current_task = None
            self.stop_requested = False
            self.stop_button.setEnabled(False)
            self.set_loading(False)

    # Инкапсулирует завершённый шаг сценария класса и возвращает результат в форме, ожидаемой следующими этапами логики.

    def stop_generation(self):
        if not self.is_generating:
            return
        self.stop_requested = True
        if self.current_task is not None and not self.current_task.done():
            self.current_task.cancel()
        async def _pause_task_after_stop():
            if not self.is_agent_connected:
                return
            try:
                task_state = await self.agent.pause_task()
                self.render_task_state(task_state)
                self.logger.info("Активная задача поставлена на паузу (STOP).")
            except Exception as e:
                self.logger.warning(f"Не удалось поставить задачу на паузу после STOP: {e}")
        asyncio.get_event_loop().create_task(_pause_task_after_stop())
        self.stop_button.setEnabled(False)
        self.set_loading(False)
        self.task_signal_label.setText("Task signal: остановлено пользователем")
        self.logger.warning("Стрим остановлен пользователем.")

    # Реакция на пользовательское событие UI: валидирует текущее состояние и запускает соответствующую бизнес-операцию.

    def on_clear_output_clicked(self):
        """
        Очищает только окно вывода (без влияния на историю на сервере).
        История в сессии не трогается.
        """
        self.output_editbox.clear()

    # === Ручное сохранение в память ===

    # Формирует отложенную запись memory_write и при доступном агенте сразу отправляет её в выбранный слой памяти ветки.

    # Реакция на пользовательское событие UI: валидирует текущее состояние и запускает соответствующую бизнес-операцию.

    def on_save_memory_clicked(self):
        layer = self.memory_layer_selector.currentData()
        key = self.memory_key_input.text().strip()
        value = self.memory_value_input.text().strip()
        if not layer:
            self.logger.warning("Не выбран слой памяти.")
            return
        if not value:
            self.logger.warning("Значение памяти пустое.")
            return

        if layer != "short_term" and not self._memory_layer_hint_logged:
            self._memory_layer_hint_logged = True
            self.logger.info(
                "short_term заполняется автоматически из диалога после каждого ответа. "
                "Селектор слоя влияет только на ручное сохранение по кнопке."
            )

        self.pending_memory_write = {
            "layer": str(layer),
            "key": key,
            "value": value,
        }

        async def _persist():
            if not self.is_agent_connected:
                self.logger.warning("Агент OFFLINE: запись памяти будет выполнена только при следующей отправке.")
                return
            try:
                payload = await self.agent.save_memory(
                    session_id=self.current_session_id,
                    branch_id=self.current_branch_id,
                    layer=str(layer),
                    key=key,
                    value=value,
                )
                self.render_memory_layers(payload.get("memory_layers") or {})
                self.memory_key_input.clear()
                self.memory_value_input.clear()
                self.pending_memory_write = None
                self.logger.success(f"Сохранено в memory layer: {layer}")
            except Exception as e:
                self.logger.warning(f"Не удалось сохранить память: {e}")

        asyncio.get_event_loop().create_task(_persist())
