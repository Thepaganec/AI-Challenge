import sys, asyncio
sys.dont_write_bytecode = True

from dotenv import load_dotenv
load_dotenv(override=True)

from qasync import QEventLoop
from PySide6.QtWidgets import QApplication
from ui.main_window import MainWindow
from core.logger.advanced_logger import Logger

def main():
    logger = Logger()
    app = QApplication(sys.argv)

    loop = QEventLoop(app)
    asyncio.set_event_loop(loop)
    
    main_window = MainWindow(logger)
    main_window.show()

    with loop:
        loop.run_forever()

if __name__ == "__main__":
    main()
