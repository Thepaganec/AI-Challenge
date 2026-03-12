import logging
import os
from datetime import datetime


def build_scheduler_logger(logs_dir: str) -> logging.Logger:
    os.makedirs(logs_dir, exist_ok=True)
    logger = logging.getLogger("scheduler_mcp")
    if logger.handlers:
        return logger

    logger.setLevel(logging.INFO)
    filename = f"scheduler_mcp_{datetime.now().strftime('%Y%m%d')}.log"
    path = os.path.join(logs_dir, filename)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    logger.propagate = False
    return logger
