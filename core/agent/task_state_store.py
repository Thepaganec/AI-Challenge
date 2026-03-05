import json
import os
from datetime import datetime
from typing import Any, Dict, List


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class TaskStateStore:
    ALLOWED_STATES = ("planning", "execution", "validation", "done")
    ALLOWED_TRANSITIONS = {
        "planning": {"execution"},
        "execution": {"planning", "validation"},
        "validation": {"execution", "done"},
        "done": set(),
    }

    def __init__(self, file_path: str):
        self.file_path = file_path
        parent = os.path.dirname(self.file_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def _default_data(self) -> Dict[str, Any]:
        return {
            "task": "",
            "state": "planning",
            "step": 0,
            "total": 0,
            "plan": [],
            "done": [],
            "current": "",
            "expected_action": "",
            "is_paused": False,
            "updated_at": _now_str(),
        }

    def _normalize_lines(self, value: Any) -> List[str]:
        if not isinstance(value, list):
            return []
        out: List[str] = []
        for item in value:
            clean = str(item or "").strip()
            if clean:
                out.append(clean)
        return out

    def _normalize(self, data: Any) -> Dict[str, Any]:
        if not isinstance(data, dict):
            return self._default_data()

        task = str(data.get("task") or "").strip()
        state = str(data.get("state") or "planning").strip().lower()
        if state not in self.ALLOWED_STATES:
            state = "planning"

        plan = self._normalize_lines(data.get("plan"))
        done = self._normalize_lines(data.get("done"))
        total = len(plan)

        step_raw = data.get("step")
        try:
            step = int(step_raw)
        except Exception:
            step = 0
        if total <= 0:
            step = 0
        elif state == "done":
            step = total
        else:
            step = min(max(step, 1), total)

        current = str(data.get("current") or "").strip()
        if not current and state in ("planning", "execution") and total > 0 and step > 0:
            current = plan[step - 1]
        if state == "validation" and not current:
            current = "Проверка результата"
        if state == "done" and not current:
            current = "Задача завершена"

        expected_action = str(data.get("expected_action") or "").strip()
        is_paused = bool(data.get("is_paused", False))

        return {
            "task": task,
            "state": state,
            "step": step,
            "total": total,
            "plan": plan,
            "done": done,
            "current": current,
            "expected_action": expected_action,
            "is_paused": is_paused,
            "updated_at": str(data.get("updated_at") or _now_str()),
        }

    def load(self) -> Dict[str, Any]:
        if not os.path.exists(self.file_path):
            return self._default_data()
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            return self._normalize(raw)
        except Exception:
            return self._default_data()

    def save(self, data: Dict[str, Any]) -> Dict[str, Any]:
        normalized = self._normalize(data)
        normalized["updated_at"] = _now_str()
        with open(self.file_path, "w", encoding="utf-8") as f:
            json.dump(normalized, f, ensure_ascii=False, indent=2)
        return normalized

    def get_state(self) -> Dict[str, Any]:
        return self.load()

    def generate_plan(self, task: str, plan: List[str]) -> Dict[str, Any]:
        clean_task = str(task or "").strip()
        if not clean_task:
            raise ValueError("task is required")
        clean_plan = self._normalize_lines(plan)
        if not clean_plan:
            raise ValueError("plan must not be empty")

        data = self._default_data()
        data["task"] = clean_task
        data["state"] = "planning"
        data["plan"] = clean_plan
        data["total"] = len(clean_plan)
        data["step"] = 1
        data["current"] = clean_plan[0]
        data["expected_action"] = "Подтвердите план, чтобы перейти к execution."
        data["done"] = []
        data["is_paused"] = False
        return self.save(data)

    def confirm_plan(self) -> Dict[str, Any]:
        data = self.load()
        if not data.get("plan"):
            raise ValueError("plan is empty")
        if str(data.get("state")) != "planning":
            raise ValueError("confirm is allowed only in planning state")
        data["state"] = "execution"
        data["step"] = max(1, int(data.get("step") or 1))
        step = int(data["step"])
        plan = data.get("plan") or []
        data["current"] = plan[step - 1] if 1 <= step <= len(plan) else ""
        data["expected_action"] = f"Выполните шаг {step}/{len(plan)}."
        return self.save(data)

    def set_paused(self, is_paused: bool) -> Dict[str, Any]:
        data = self.load()
        data["is_paused"] = bool(is_paused)
        return self.save(data)

    def transition(self, target_state: str, data: Dict[str, Any] | None = None) -> Dict[str, Any]:
        clean_target = str(target_state or "").strip().lower()
        if clean_target not in self.ALLOWED_STATES:
            raise ValueError("invalid target state")

        data = data if isinstance(data, dict) else self.load()
        source = str(data.get("state") or "planning")
        allowed = self.ALLOWED_TRANSITIONS.get(source, set())
        if clean_target not in allowed:
            raise ValueError(f"transition {source} -> {clean_target} is forbidden")

        data["state"] = clean_target
        if clean_target == "execution":
            if int(data.get("total") or 0) > 0:
                step = int(data.get("step") or 1)
                step = min(max(step, 1), int(data["total"]))
                data["step"] = step
                data["current"] = (data.get("plan") or [])[step - 1]
                data["expected_action"] = f"Выполните шаг {step}/{int(data['total'])}."
        elif clean_target == "planning":
            data["expected_action"] = "Уточните/обновите план и подтвердите его."
        elif clean_target == "validation":
            data["current"] = "Проверка результата"
            data["expected_action"] = "Проверьте соответствие результата плану."
        elif clean_target == "done":
            data["step"] = int(data.get("total") or 0)
            data["current"] = "Задача завершена"
            data["expected_action"] = ""
        return self.save(data)

    def next_step(self) -> Dict[str, Any]:
        data = self.load()
        if bool(data.get("is_paused")):
            raise ValueError("task is paused")
        state = str(data.get("state") or "")
        plan = data.get("plan") or []
        total = int(data.get("total") or len(plan))
        step = int(data.get("step") or 0)

        if state == "execution":
            current = str(data.get("current") or "").strip()
            if current:
                done = data.get("done") if isinstance(data.get("done"), list) else []
                done.append(current)
                data["done"] = self._normalize_lines(done)
            if step < total:
                step += 1
                data["step"] = step
                data["current"] = plan[step - 1]
                data["expected_action"] = f"Выполните шаг {step}/{total}."
                return self.save(data)
            return self.transition("validation", data=data)

        if state == "validation":
            return self.transition("done", data=data)

        raise ValueError("next_step is allowed only in execution/validation")

    def update_progress(
        self,
        *,
        current: str = "",
        expected_action: str = "",
        done_item: str = "",
        step: int | None = None,
    ) -> Dict[str, Any]:
        data = self.load()
        if current.strip():
            data["current"] = current.strip()
        if expected_action.strip():
            data["expected_action"] = expected_action.strip()
        if done_item.strip():
            done = data.get("done") if isinstance(data.get("done"), list) else []
            done.append(done_item.strip())
            data["done"] = self._normalize_lines(done)
        if step is not None:
            total = int(data.get("total") or 0)
            if total > 0:
                data["step"] = min(max(int(step), 1), total)
        return self.save(data)
