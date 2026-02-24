import sys, os, asyncio, shutil
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

from dotenv import load_dotenv
load_dotenv(override=True)

from qasync import QEventLoop
from PySide6.QtWidgets import QApplication
from ui.main_window import MainWindow
from core.logger.advanced_logger import Logger

def main():
    remove_pycache(os.path.dirname(os.path.abspath(__file__)))

    logger = Logger()
    app = QApplication(sys.argv)

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    
    main_window = MainWindow(logger)
    main_window.show()

    with loop:
        loop.run_forever()

def remove_pycache(root_dir: str):
    try:
        for dirpath, dirnames, _ in os.walk(root_dir):
            if "__pycache__" in dirnames:
                pyc_path = os.path.join(dirpath, "__pycache__")
                try:
                    shutil.rmtree(pyc_path, ignore_errors=True)
                except Exception:
                    pass
    except Exception:
        pass

if __name__ == "__main__":
    main()
