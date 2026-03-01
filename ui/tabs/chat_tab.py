import os, json, asyncio, time
import uuid
import extra.Global as Global

from core.logger.advanced_logger import Logger
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QSizePolicy, QProgressBar, QSplitter, QLabel,
    QLineEdit, QPushButton, QComboBox, QDoubleSpinBox, QListWidget, QListWidgetItem,
    QSpinBox
)

from PySide6.QtCore import (Qt, QByteArray, QTimer, QEvent)
from PySide6.QtGui import QTextCursor

from ui.tabs.base_tab import BaseTab
from ui.widgets.input_controller import InputController
from ui.widgets.output_controller import OutputController
from ui.widgets.parameters_controller import ParametersController
from ui.widgets.metrics_controller import MetricsController
from ui.widgets.sessions_controller import SessionsController
from ui.widgets.summarization_controller import SummarizationController
from ui.widgets.API_Controllers import APIControllers
from core.agent.agent_client import AgentClient


class ChatTab(BaseTab):
    path = os.path.dirname(__file__)
    file_name = f"{os.path.splitext(os.path.basename(__file__))[0]}.json"
    CONFIG_FILE = os.path.join(path, file_name)

    def __init__(self, logger: Logger):
        super().__init__(logger)

        self.agent = AgentClient(logger=logger)

        # --- Служебные
        self.is_generating = False
        self.stop_requested = False
        self.current_task = None

        self.init_content()
        self.load_window_state()

        self.outbox.set_length_threshold(self.condition_parameters.char_limit_input.value())

        self.splitter_move_timer = QTimer(self)
        self.splitter_move_timer.setSingleShot(True)
        
        # ============ СЛУШАЕМ КРИКИ
        self.log_splitter.splitterMoved.connect(self.on_splitter_moved)
        self.horizontal_splitter.splitterMoved.connect(self.on_splitter_moved)
        self.splitter_move_timer.timeout.connect(self.save_window_state)
        #self.condition_toggle.toggled.connect(self.condition_toggle_changed)
        self.condition_parameters.char_limit_changed.connect(self.on_char_limit_changed)

    def init_content(self):
        # ============ ОБЪЕКТЫ ВКЛАДКИ
        self.inbox = InputController(agent=self.agent, logger=self.logger)
        self.inbox.setContentsMargins(150, 0, 150, 0)

        self.outbox = OutputController()
        self.outbox.setContentsMargins(0, 0, 0, 0) 

        self.session_list = SessionsController(agent=self.agent, logger=self.logger) 
        self.session_list.setContentsMargins(0, 0, 0, 0) 

        self.condition_parameters = ParametersController(logger=self.logger)
        self.condition_parameters.setContentsMargins(0, 0, 0, 0) 

        self.metrics = MetricsController()
        self.metrics.setContentsMargins(0, 0, 0, 0) 

        self.summary = SummarizationController(logger=self.logger)
        self.summary.setContentsMargins(0, 0, 0, 0) 

        # ============ РАССТАНОВКА ЭЛЕМЕНТОВ
        left_panel_container = QWidget()
        left_panel = QVBoxLayout(left_panel_container)
        left_panel.addWidget(self.session_list)
        left_panel.addWidget(self.inbox)
        left_panel.addWidget(self.outbox)
        left_panel.addStretch()

        right_panel_container = QWidget()
        right_panel = QVBoxLayout(right_panel_container)
        right_panel.addWidget(self.condition_parameters)
        right_panel.addWidget(self.metrics)
        right_panel.addWidget(self.summary)
        right_panel.addStretch()

        self.horizontal_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.horizontal_splitter.addWidget(left_panel_container)
        self.horizontal_splitter.addWidget(right_panel_container)

        widget_layout = QVBoxLayout(self.top_widget)
        widget_layout.addWidget(self.horizontal_splitter)
    
    def on_char_limit_changed(self, value):
        self.outbox.set_length_threshold(value)




















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

    # ===================================
    def on_splitter_moved(self):
        self.splitter_move_timer.start(300)

    def save_window_state(self):
        try:
            state = {}
            if hasattr(self, "log_splitter"):
                state["log_splitter"] = self.log_splitter.saveState().toHex().data().decode()

            if hasattr(self, "horizontal_splitter"):
                state["horizontal_splitter"] = self.horizontal_splitter.saveState().toHex().data().decode()

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
                
            if "horizontal_splitter" in state:
                try:
                    splitter_state = QByteArray.fromHex(str(state["horizontal_splitter"]).encode())
                    self.horizontal_splitter.restoreState(splitter_state)
                except Exception as e:
                    self.logger.error(f"Ошибка восстановления состояния horizontal_splitter вкладки \"Chat_tab\": {e}")
                    return

        except Exception as e:
            self.logger.error(f"Ошибка загрузки состояния окна для вкладки \"Chat_tab\": {e}")
            return

        self.logger.debug("Состояние вкладки загружено")
