"""PhotoCuller 入口"""
from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from photo_culler.config import Config
from photo_culler.ui.mainwindow import MainWindow

# 回收目录默认位置：桌面 PhotoCuller\回收站
DEFAULT_TRASH = Path.home() / "Desktop" / "PhotoCuller" / "回收站"


def ensure_trash_root(cfg: Config) -> None:
    """回收目录为空时，自动设为桌面 PhotoCuller\\回收站 并告知一次。"""
    if cfg.trash_root is not None:
        return
    DEFAULT_TRASH.mkdir(parents=True, exist_ok=True)
    cfg.set("trash_root", str(DEFAULT_TRASH))
    QMessageBox.information(
        None, "回收目录已设置",
        f"被移走的照片将统一存放到：\n{DEFAULT_TRASH}\n\n"
        "随时可通过「回收站管理」恢复或彻底清理。")


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("PhotoCuller")
    cfg = Config()
    ensure_trash_root(cfg)
    win = MainWindow(cfg)
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
