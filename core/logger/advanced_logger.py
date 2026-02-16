import os, logging
from pathlib import Path
from datetime import datetime, timedelta
from PySide6.QtCore import QObject, Signal
from logging.handlers import RotatingFileHandler

# --- Кастомный уровень SUCCESS ---
SUCCESS_LEVEL_NUM = 25  # Между INFO (20) и WARNING (30)
logging.addLevelName(SUCCESS_LEVEL_NUM, "SUCCESS")

def _success(self, message, *args, **kwargs):
    """Метод для logging.Logger: logger.success(...)."""
    if self.isEnabledFor(SUCCESS_LEVEL_NUM):
        self._log(SUCCESS_LEVEL_NUM, message, args, **kwargs)

logging.Logger.success = _success
# --- конец блока кастомного уровня ---

class Logger(QObject):
    log_signal = Signal(str, str)  # Сигнал для GUI (текст, цвет)

    def __init__(self):
        super().__init__()

        self.log_dir = Path("logs")
        self.log_dir.mkdir(exist_ok=True)
        self.setup_logging()
        self.clean_old_logs()

    def setup_logging(self):
        log_file = self.log_dir / f"bot_{datetime.now().strftime('%Y%m%d')}.log"
        self.logger = logging.getLogger("Ai Challenge")
        self.logger.setLevel(logging.DEBUG)

        formatter = logging.Formatter(
            '%(asctime)s - %(levelname)s \t %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

        file_handler = RotatingFileHandler(
            log_file, maxBytes=5 * 1024 * 1024, backupCount=5
        )
        file_handler.setFormatter(formatter)
        self.logger.addHandler(file_handler)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        self.logger.addHandler(console_handler)

    def clean_old_logs(self):
        now = datetime.now()
        for log_file in self.log_dir.glob("bot_*.log"):
            try:
                file_date = datetime.strptime(log_file.stem[4:], "%Y%m%d")
                if (now - file_date) > timedelta(days=7):
                    os.remove(log_file)
            except ValueError:
                continue

    def log(self, level, message):
        colors = {
            'debug': 'gray',
            'info': 'white',
            'warning': 'orange',
            'error': 'red',
            'critical': 'darkred',
            'success': '#39FF14'
        }

        log_message = (
            f"{datetime.now().strftime('%H:%M:%S')} - {level.upper()} \t {message}"
        )

        self.log_signal.emit(log_message, colors.get(level, 'white'))
        extra = ""

        # В файл/консоль уходит "сырое" сообщение
        if level == "success":
            # Для success используем кастомный уровень
            self.logger.success(message, extra=extra)
        else:
            # debug/info/warning/error/critical
            log_method = getattr(self.logger, level, None)
            if log_method is None:
                self.logger.error(f"Unknown log level '{level}': {message}", extra=extra)
            else:
                log_method(message, extra=extra)

    def debug(self, message):
        self.log('debug', message)

    def info(self, message):
        self.log('info', message)

    def warning(self, message):
        self.log('warning', message)

    def error(self, message):
        self.log('error', message)

    def critical(self, message):
        self.log('critical', message)

    def success(self, message):
        self.log('success', message)

    def error_handler(self, e, context=""):
        error_msg = f"{context}: {type(e).__name__}: {str(e)}"
        self.error(error_msg)
        return error_msg
