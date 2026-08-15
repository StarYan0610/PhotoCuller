"""回收站管理窗口：查看历史删除记录、恢复、彻底删除、打开目录"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from ..core import trash as trashmod
from ..core.models import human_size


class TrashWindow(QDialog):
    def __init__(self, trash_root: Path, file_worker, parent=None):
        super().__init__(parent)
        self.setWindowTitle("回收站管理")
        self.resize(760, 480)
        self._trash_root = trash_root
        self._worker = file_worker

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"回收目录: {trash_root}"))

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["时间", "文件名", "大小", "状态", "原目录"])
        self.tree.setColumnWidth(0, 150)
        self.tree.setColumnWidth(1, 220)
        self.tree.setColumnWidth(2, 80)
        self.tree.setColumnWidth(3, 80)
        layout.addWidget(self.tree)

        btns = QHBoxLayout()
        self.btn_restore = QPushButton("恢复选中")
        self.btn_purge = QPushButton("彻底删除选中")
        self.btn_open = QPushButton("打开回收目录")
        self.btn_refresh = QPushButton("刷新")
        btn_close = QPushButton("关闭")
        btns.addWidget(self.btn_restore)
        btns.addWidget(self.btn_purge)
        btns.addWidget(self.btn_open)
        btns.addWidget(self.btn_refresh)
        btns.addStretch(1)
        btns.addWidget(btn_close)
        layout.addLayout(btns)

        self.btn_restore.clicked.connect(self._restore_selected)
        self.btn_purge.clicked.connect(self._purge_selected)
        self.btn_open.clicked.connect(self._open_dir)
        self.btn_refresh.clicked.connect(self.reload)
        btn_close.clicked.connect(self.accept)

        self.reload()

    def reload(self) -> None:
        self.tree.clear()
        self._records: dict[str, dict] = {}
        for rec in trashmod.load_history(self._trash_root):
            status = trashmod.record_disk_status(rec)
            status_text = {"in_trash": "在回收目录", "restored": "已恢复",
                           "missing": "已清空"}[status]
            total = sum(f.get("size", 0) for f in rec.get("files", []))
            names = ", ".join(f["name"] for f in rec.get("files", []))
            item = QTreeWidgetItem([
                rec.get("time", ""),
                f"{rec.get('basename', '')}  ({names})",
                human_size(total),
                status_text,
                rec.get("src_dir", ""),
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, rec["id"])
            if status != "in_trash":
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
                for c in range(5):
                    item.setForeground(c, self.palette().disabled().windowText())
            self.tree.addTopLevelItem(item)
            self._records[rec["id"]] = rec

    def _selected_record(self) -> dict | None:
        items = self.tree.selectedItems()
        if not items:
            return None
        rid = items[0].data(0, Qt.ItemDataRole.UserRole)
        return self._records.get(rid)

    def _restore_selected(self) -> None:
        rec = self._selected_record()
        if not rec:
            return
        self._worker.enqueue_restore(rec)
        QMessageBox.information(self, "恢复", "已加入恢复队列，完成后列表会自动刷新。")

    def _purge_selected(self) -> None:
        rec = self._selected_record()
        if not rec:
            return
        total = sum(f.get("size", 0) for f in rec.get("files", []))
        ret = QMessageBox.warning(
            self, "彻底删除",
            f"将永久删除 {rec.get('basename', '')} 的回收副本（{human_size(total)}），\n"
            "此操作不可恢复！确定继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if ret != QMessageBox.StandardButton.Yes:
            return
        trash_dir = Path(rec["trash_dir"])
        try:
            # 逐文件删除（兼容平铺布局：trash_dir 即回收根目录，绝不能整目录 rmtree）
            for finfo in rec.get("files", []):
                f = trash_dir / finfo.get("name", "")
                try:
                    if f.exists():
                        f.unlink()
                except OSError:
                    pass
            # 旧嵌套布局：清空后移除空的组子目录
            if trash_dir != self._trash_root and trash_dir.exists() \
                    and not any(trash_dir.iterdir()):
                trash_dir.rmdir()
            trashmod.append_record(self._trash_root,
                                   trashmod.new_restore_record(rec["id"]))
        except OSError as e:
            QMessageBox.critical(self, "错误", f"删除失败: {e}")
        self.reload()

    def _open_dir(self) -> None:
        os.startfile(str(self._trash_root))  # noqa: S606 Windows 资源管理器
