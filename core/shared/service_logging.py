import json
import logging
import os
from datetime import datetime
from typing import Any, Optional


class ServiceLogger:
    def __init__(self, logger: logging.Logger):
        self._logger = logger

    def _format(self, event: str, payload: Optional[Any] = None) -> str:
        if payload is None:
            return event
        if isinstance(payload, str):
            return f"{event}\n{payload}"
        try:
            body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
        except Exception:
            body = str(payload)
        return f"{event}\n{body}"

    def debug(self, event: str, payload: Optional[Any] = None) -> None:
        self._logger.debug(self._format(event, payload))

    def info(self, event: str, payload: Optional[Any] = None) -> None:
        self._logger.info(self._format(event, payload))

    def warning(self, event: str, payload: Optional[Any] = None) -> None:
        self._logger.warning(self._format(event, payload))

    def error(self, event: str, payload: Optional[Any] = None) -> None:
        self._logger.error(self._format(event, payload))

    def exception(self, event: str, payload: Optional[Any] = None) -> None:
        self._logger.exception(self._format(event, payload))


def build_service_logger(service_name: str, logs_root: str) -> ServiceLogger:
    os.makedirs(logs_root, exist_ok=True)
    service_log_dir = os.path.join(logs_root, service_name)
    os.makedirs(service_log_dir, exist_ok=True)

    logger_name = f"service.{service_name}"
    logger = logging.getLogger(logger_name)
    if not logger.handlers:
        logger.setLevel(logging.INFO)
        path = os.path.join(service_log_dir, f"{service_name}_{datetime.now().strftime('%Y%m%d')}.log")
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

        file_handler = logging.FileHandler(path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
        logger.propagate = False

    return ServiceLogger(logger)
