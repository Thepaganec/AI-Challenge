import os, sys
sys.dont_write_bytecode = True

from datetime import datetime, timedelta
from typing import Optional


class AgentFileLogger:
    def __init__(self, logs_dir: str, prefix: str = "agentlogs"):
        self.logs_dir = logs_dir
        self.prefix = prefix
        os.makedirs(self.logs_dir, exist_ok=True)

    def _log_path_for_today(self) -> str:
        day = datetime.now().strftime("%Y%m%d")
        return os.path.join(self.logs_dir, f"{self.prefix}{day}.txt")

    def cleanup_old_logs(self, keep_days: int = 3) -> None:
        cutoff = datetime.now() - timedelta(days=keep_days)
        try:
            for name in os.listdir(self.logs_dir):
                if not (name.startswith(self.prefix) and name.endswith(".txt")):
                    continue

                path = os.path.join(self.logs_dir, name)
                try:
                    mtime = datetime.fromtimestamp(os.path.getmtime(path))
                except Exception:
                    continue

                if mtime < cutoff:
                    try:
                        os.remove(path)
                    except Exception:
                        pass
        except Exception:
            pass

    def write(self, level: str, message: str, extra: Optional[str] = None) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] [{level.upper()}] {message}"
        if extra:
            line += f" | {extra}"
        line += "\n"

        path = self._log_path_for_today()
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass