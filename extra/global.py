from PySide6.QtWidgets import (QTextEdit)

def set_editbox_height(editbox: QTextEdit, lines: int):
    fm = editbox.fontMetrics()
    line_height = fm.lineSpacing()

    extra = (
        int(editbox.document().documentMargin() * 2)
        + int(editbox.frameWidth() * 2)
        + 12
    )

    editbox.setFixedHeight(line_height * lines + extra)