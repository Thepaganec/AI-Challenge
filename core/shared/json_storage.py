import json
import os
import tempfile
from copy import deepcopy
from typing import Any, Dict


class JsonFileStore:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        os.makedirs(self.base_dir, exist_ok=True)

    def ensure_file(self, path: str, default_payload: Dict[str, Any]) -> None:
        if os.path.exists(path):
            return
        self.write_json(path, default_payload)

    def read_json(self, path: str, default_payload: Dict[str, Any]) -> Dict[str, Any]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        return deepcopy(default_payload)

    def write_json(self, path: str, payload: Dict[str, Any]) -> None:
        fd, tmp_path = tempfile.mkstemp(prefix="json_store_", suffix=".json", dir=self.base_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as tmp_file:
                json.dump(payload, tmp_file, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
