from PySide6.QtWidgets import QTextEdit

def set_editbox_height(edit: QTextEdit, lines: int = 6):
    """
    Маленький хелпер: фиксируем высоту QTextEdit приблизительно под N строк.
    """
    try:
        fm = edit.fontMetrics()
        h = fm.lineSpacing() * int(lines) + 16
        edit.setFixedHeight(int(h))
    except Exception:
        pass
