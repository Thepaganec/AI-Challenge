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
from core.agent.agent_client import AgentClient
from extra.global_utils import set_editbox_height


class ChatTab(BaseTab):
    path = os.path.dirname(__file__)
    file_name = f"{os.path.splitext(os.path.basename(__file__))[0]}.json"
    CONFIG_FILE = os.path.join(path, file_name)

    def __init__(self, logger):
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

    async def refresh_sessions_list_offline_first(self):
        # сразу покажем текущую сессию (даже если агент оффлайн)
        self.render_sessions_list_offline()
        await asyncio.sleep(0.1)
        if self.is_agent_connected:
            await self.refresh_sessions_list()

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

        self.keep_last_n_input = QSpinBox()
        self.keep_last_n_input.setRange(1, 100)
        self.keep_last_n_input.setValue(10)
        self.keep_last_n_input.setFixedWidth(120)

        self.strategy_selector = QComboBox()
        self.strategy_selector.setFixedWidth(240)
        self.strategy_selector.addItem("Sliding Window (последние N)", "sliding")
        self.strategy_selector.addItem("Sticky Facts + последние N", "facts")
        self.strategy_selector.addItem("Branching (ветки) + последние N", "branching")
        self.strategy_selector.currentIndexChanged.connect(self.on_strategy_changed)

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
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(8)

        self.output_editbox = QTextEdit()
        self.output_editbox.setFont(font)
        self.output_editbox.setReadOnly(True)

        self.stop_button = QPushButton("STOP")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_generation)

        self.clear_button = QPushButton("CLEAR")
        self.clear_button.clicked.connect(self.output_editbox.clear)

        btn_row = QWidget()
        br = QHBoxLayout(btn_row)
        br.setContentsMargins(0, 0, 0, 0)
        br.addWidget(self.stop_button)
        br.addStretch()
        br.addWidget(self.clear_button)

        # ===== metrics =====
        self.metrics_box = QTextEdit()
        self.metrics_box.setReadOnly(True)
        self.metrics_box.setMinimumHeight(160)
        self.metrics_box.setPlaceholderText("usage / cost / стратегия / ветка ...")

        self.facts_box = QTextEdit()
        self.facts_box.setReadOnly(True)
        self.facts_box.setMinimumHeight(140)
        self.facts_box.setPlaceholderText("FACTS для стратегии Sticky Facts (показывается, когда она активна).")

        # ===== layout =====
        top_controls = QGroupBox("Параметры запроса")
        tc = QVBoxLayout(top_controls)
        tc.setContentsMargins(8, 8, 8, 8)
        tc.setSpacing(6)

        def row(lbl, widget):
            w = QWidget()
            l = QHBoxLayout(w)
            l.setContentsMargins(0, 0, 0, 0)
            l.setSpacing(8)
            l.addWidget(QLabel(lbl))
            l.addStretch()
            l.addWidget(widget)
            return w

        tc.addWidget(row("Модель:", self.model_selector))
        tc.addWidget(row("Эндпоинт:", self.endpoint_selector))
        tc.addWidget(row("temperature:", self.temperature_input))
        tc.addWidget(row("N сообщений (user+assistant):", self.keep_last_n_input))
        tc.addWidget(row("Стратегия контекста:", self.strategy_selector))

        branching_box = QGroupBox("Branching (ветки диалога)")
        bb = QVBoxLayout(branching_box)
        bb.setContentsMargins(8, 8, 8, 8)
        bb.setSpacing(6)

        bb.addWidget(row("Текущая ветка:", self.branch_selector))
        bb.addWidget(row("Checkpoint:", self.checkpoint_name))
        bb.addWidget(self.create_checkpoint_btn)
        bb.addSpacing(6)
        bb.addWidget(row("Выбрать checkpoint:", self.checkpoint_selector))
        bb.addWidget(row("Новая ветка:", self.new_branch_name))
        bb.addWidget(self.create_branch_btn)

        right_panel = QWidget()
        rp = QVBoxLayout(right_panel)
        rp.setContentsMargins(0, 0, 0, 0)
        rp.setSpacing(10)
        rp.addWidget(top_controls)
        rp.addWidget(branching_box)
        rp.addWidget(QLabel("Metrics:"))
        rp.addWidget(self.metrics_box)
        rp.addWidget(QLabel("Facts:"))
        rp.addWidget(self.facts_box)
        rp.addStretch()

        left_panel = QWidget()
        lp = QVBoxLayout(left_panel)
        lp.setContentsMargins(0, 0, 0, 0)
        lp.setSpacing(10)
        lp.addWidget(session_box)
        lp.addWidget(QLabel("Ввод:"))
        lp.addWidget(self.input_editbox)
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

    # ======= UI helpers =======
    def set_loading(self, is_loading: bool):
        if is_loading:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 1)
            self.progress_bar.setValue(0)

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

    async def preload_agent_status(self):
        try:
            self.logger.info("Подключение к агенту (локальный сервер)...")
            ok = await self.agent.ping()
            self.is_agent_connected = bool(ok)
            if self.is_agent_connected:
                self.logger.success("Агент найден: подключение успешно")
                await self.refresh_sessions_list()
                await self.load_session_to_ui(self.current_session_id)
            else:
                self.logger.warning("Агент не отвечает. Запусти core/agent/agent_server.py перед UI.")
        except Exception as e:
            self.is_agent_connected = False
            self.logger.warning(f"Не удалось подключиться к агенту: {e}")

    def render_sessions_list_offline(self):
        self.sessions_list.blockSignals(True)
        self.sessions_list.clear()
        item = QListWidgetItem(f"{self.current_session_id} — (текущая, новая)")
        item.setData(Qt.UserRole, self.current_session_id)
        self.sessions_list.addItem(item)
        self.sessions_list.blockSignals(False)

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

    def on_session_clicked(self, item: QListWidgetItem):
        sid = item.data(Qt.UserRole)
        if not sid or self.is_generating:
            return
        self.current_session_id = str(sid)
        asyncio.get_event_loop().create_task(self.load_session_to_ui(self.current_session_id))

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

        self.output_editbox.clear()
        self.metrics_box.clear()
        self.facts_box.clear()

        # restore active branch
        active_branch = (session.get("active_branch") or "main").strip() or "main"
        self.current_branch_id = active_branch

        branches = session.get("branches") or {}
        if not isinstance(branches, dict):
            branches = {}

        # fill branch selector
        self.branch_selector.blockSignals(True)
        self.branch_selector.clear()
        for bid, b in branches.items():
            name = (b.get("name") or bid).strip()
            self.branch_selector.addItem(f"{name} ({bid})", bid)
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

        # show facts if exists
        facts = branch.get("facts") if isinstance(branch.get("facts"), dict) else {}
        self.render_facts(facts)

        self.logger.debug(f"Сессия загружена. session={session_id} branch={self.current_branch_id}")

    def refresh_checkpoints_ui(self, branch: dict):
        cps = branch.get("checkpoints") if isinstance(branch.get("checkpoints"), dict) else {}
        self.checkpoint_selector.clear()
        for cp_id in cps.keys():
            self.checkpoint_selector.addItem(cp_id, cp_id)

    def render_facts(self, facts: dict):
        if not isinstance(facts, dict) or not facts:
            self.facts_box.setPlainText("")
            return
        lines = [f"{k}: {v}" for k, v in facts.items()]
        self.facts_box.setPlainText("\n".join(lines))

    def on_new_session_clicked(self):
        if self.is_generating:
            self.logger.warning("Нельзя сменить сессию во время генерации.")
            return
        self.current_session_id = str(uuid.uuid4())
        self.current_branch_id = "main"
        self.output_editbox.clear()
        self.metrics_box.clear()
        self.facts_box.clear()
        self.render_sessions_list_offline()
        if self.is_agent_connected:
            asyncio.get_event_loop().create_task(self.refresh_sessions_list())
        self.logger.success(f"Создана новая сессия: {self.current_session_id}")

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
                    self.output_editbox.clear()
                    self.metrics_box.clear()
                    self.facts_box.clear()
                    self.current_branch_id = "main"
                    await self.refresh_sessions_list()
                    await self.load_session_to_ui(self.current_session_id)
                    self.logger.success("История удалена.")
                else:
                    self.logger.warning("Не удалось удалить историю (agent вернул False).")
            except Exception as e:
                self.logger.warning(f"Ошибка удаления истории: {e}")

        asyncio.get_event_loop().create_task(_do())

    def on_model_changed(self, model_text: str):
        model_text = (model_text or "").strip()
        is_gpt52_locked = (model_text == "gpt-5.2-chat-latest")
        self.temperature_input.setEnabled(not is_gpt52_locked)
        if is_gpt52_locked:
            self.temperature_input.setValue(1.0)
            self.logger.warning("Для gpt-5.2-chat-latest temperature заблокирована ProxyAPI. Установлено 1.0.")

    def on_strategy_changed(self):
        strategy = self.strategy_selector.currentData()
        is_branching = (strategy == "branching")
        # блок веток доступен всегда, но подсвечивать можно логами
        if is_branching:
            self.logger.info("Стратегия: Branching. История ведётся по выбранной ветке.")
        elif strategy == "facts":
            self.logger.info("Стратегия: Sticky Facts. Facts обновляются после каждого сообщения пользователя.")
        else:
            self.logger.info("Стратегия: Sliding Window. В модель отправляются только последние N сообщений.")

    def on_branch_changed(self):
        if self.is_generating:
            return
        bid = self.branch_selector.currentData()
        if not bid:
            return
        # переключение ветки происходит на сервере, чтобы сохранялось на диск
        asyncio.get_event_loop().create_task(self._switch_branch_async(str(bid)))

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

    def on_create_checkpoint_clicked(self):
        if self.is_generating:
            return
        asyncio.get_event_loop().create_task(self._create_checkpoint_async())

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

    def on_create_branch_clicked(self):
        if self.is_generating:
            return
        asyncio.get_event_loop().create_task(self._create_branch_async())

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
        keep_last_n = int(self.keep_last_n_input.value())
        strategy = self.strategy_selector.currentData()

        self.current_task = asyncio.create_task(
            self.ask_and_stream_answer(
                user_text=text,
                model=selected_model,
                endpoint=selected_endpoint,
                temperature=selected_temperature,
                keep_last_n=keep_last_n,
                strategy=strategy,
            )
        )

    async def ask_and_stream_answer(self, user_text: str, model: str, endpoint: str, temperature, keep_last_n: int, strategy: str):
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

            gen = self.agent.stream_chat(
                user_text=user_text,
                model=model,
                endpoint=endpoint,
                max_tokens=800,
                temperature=temperature,
                session_id=self.current_session_id,
                branch_id=self.current_branch_id,
                keep_last_n=keep_last_n,
                context_strategy=strategy,
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
            active_branch = getattr(self.agent, "last_active_branch", None) or self.current_branch_id
            self.current_branch_id = active_branch

            strategy_used = ms.get("strategy") or strategy
            facts_count = ms.get("facts_count")

            line = (
                f"Strategy={strategy_used} | Branch={active_branch} | keep_last_n={keep_last_n} | "
                f"TTFT={ttft_str} | Total={total_sec:.3f}s | "
                f"prompt={prompt_tokens} | completion={completion_tokens} | total={total_tokens_call} | Cost={cost_str} | "
                f"Temp={temp_str}"
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

            # refresh session list (title updates) — БЕЗ перезагрузки UI (иначе чистит поля)
            if self.is_agent_connected:
                asyncio.get_event_loop().create_task(self.refresh_sessions_list())

            self.is_generating = False
            self.current_task = None
            self.stop_requested = False
            self.stop_button.setEnabled(False)
            self.set_loading(False)

    def stop_generation(self):
        if not self.is_generating:
            return
        self.stop_requested = True
        if self.current_task is not None and not self.current_task.done():
            self.current_task.cancel()
        self.stop_button.setEnabled(False)
        self.set_loading(False)
        self.logger.warning("Стрим остановлен пользователем.")

    def on_splitter_moved(self):
        self.splitter_move_timer.start(300)

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
