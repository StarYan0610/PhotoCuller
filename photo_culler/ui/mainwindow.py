"""主窗口：浏览、缩放、复制、外部打开、标记复制、胶片栏、删除、撤销"""
from __future__ import annotations

import os
import subprocess
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, QMimeData, QSize, Qt, QUrl, QTimer
from PySide6.QtGui import (
    QColor, QCursor, QGuiApplication, QIcon, QImage, QKeyEvent, QPainter, QPixmap,
)
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView,
    QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QProgressBar, QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from ..config import Config
from ..core import filters
from ..core import trash as trashmod
from ..core.exif import format_exposure, read_exposure
from ..core.fileops import FileOpWorker
from ..core.imageloader import ImageCache, Prefetcher, decode_image
from ..core.models import GroupState, PhotoGroup, ReviewMode, human_size
from ..core.scanner import scan_directory
from ..core.undo import DeleteCommand, UndoStack
from .trashwindow import TrashWindow

THUMB_W, THUMB_H = 160, 160  # 胶片栏缩略图解码尺寸

# 评分多选菜单的固定选项：(星值, 显示文本)
RATING_OPTIONS: list[tuple[int, str]] = [
    (0, "未评分"),
    (1, "★ 1 星"),
    (2, "★★ 2 星"),
    (3, "★★★ 3 星"),
    (4, "★★★★ 4 星"),
    (5, "★★★★★ 5 星"),
]


class CheckableMenu(QMenu):
    """多选菜单：点击勾选项后保持展开，便于连续勾选；点菜单外才关闭。"""

    def mouseReleaseEvent(self, event) -> None:
        action = self.activeAction()
        if action is not None and action.isEnabled():
            action.trigger()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ImageView(QGraphicsView):
    """图片视图：适应窗口/缩放/拖拽平移、悬停左右箭头、右键菜单"""

    _BTN_STYLE = (
        "QToolButton { background: rgba(70,70,70,150); color: white;"
        " border-radius: 24px; font-size: 28px; font-weight: bold; padding-bottom: 4px; }"
        "QToolButton:hover { background: rgba(100,100,100,210); }"
    )

    def __init__(self):
        super().__init__()
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._item = QGraphicsPixmapItem()
        self._scene.addItem(self._item)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        self.setBackgroundBrush(QColor(26, 26, 26))
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport().setMouseTracking(True)

        self._fit = True
        self.on_prev = None        # 回调：左箭头
        self.on_next = None        # 回调：右箭头
        self.on_toggle_mark = None  # 回调：鼠标中键标记/取消标记
        self.on_need_full = None   # 回调：请求全尺寸解码
        self.menu_builder = None   # 回调：返回 QMenu

        self._placeholder = QLabel("", self)
        self._placeholder.setStyleSheet("color: #888888; font-size: 15px;")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._placeholder.hide()

        self.btn_prev = self._make_arrow("‹")
        self.btn_next = self._make_arrow("›")
        self.btn_prev.clicked.connect(self._click_prev)
        self.btn_next.clicked.connect(self._click_next)

    # ---- 箭头 ----
    def _make_arrow(self, text: str) -> QToolButton:
        btn = QToolButton(self)
        btn.setText(text)
        btn.setFixedSize(48, 48)
        btn.setStyleSheet(self._BTN_STYLE)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        btn.hide()
        return btn

    def _click_prev(self) -> None:
        if self.on_prev:
            self.on_prev()
        self.setFocus()

    def _click_next(self) -> None:
        if self.on_next:
            self.on_next()
        self.setFocus()

    # ---- 图片设置 ----
    def show_image(self, img: QImage | None, placeholder: str = "") -> None:
        """设置新图并重置为适应窗口视图。"""
        if img is None:
            self._item.setPixmap(QPixmap())
            self._scene.setSceneRect(0, 0, 1, 1)
            self._placeholder.setText(placeholder or "（无预览）")
            self._placeholder.setVisible(True)
            return
        self._placeholder.hide()
        self._item.setPixmap(QPixmap.fromImage(img))
        self._scene.setSceneRect(0, 0, img.width(), img.height())
        self.fit()

    def replace_keep_view(self, img: QImage) -> None:
        """全尺寸解码完成后原位替换，保持当前缩放与视野中心。"""
        center = self.mapToScene(self.viewport().rect().center())
        transform = self.transform()
        self._item.setPixmap(QPixmap.fromImage(img))
        self._scene.setSceneRect(0, 0, img.width(), img.height())
        self.setTransform(transform)
        self.centerOn(center)

    # ---- 缩放 ----
    def fit(self) -> None:
        self._fit = True
        if not self._item.pixmap().isNull():
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)

    def zoom_100(self, scene_pos=None) -> None:
        if self._item.pixmap().isNull():
            return
        self._fit = False
        self.resetTransform()
        if scene_pos is not None:
            self.centerOn(scene_pos)
        if self.on_need_full:
            self.on_need_full()

    def current_scale(self) -> float:
        return self.transform().m11()

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if self._item.pixmap().isNull():
                return
            factor = 1.25 if event.angleDelta().y() > 0 else 0.8
            self._fit = False
            self.scale(factor, factor)
            if self.current_scale() > 1.0 and self.on_need_full:
                self.on_need_full()
            event.accept()
        else:
            # 无 Ctrl：滚轮上下切换上一张/下一张
            if event.angleDelta().y() > 0:
                if self.on_prev:
                    self.on_prev()
            else:
                if self.on_next:
                    self.on_next()
            event.accept()

    def mousePressEvent(self, event) -> None:
        # 鼠标中键（按下滚轮）= 标记保留/取消标记
        if event.button() == Qt.MouseButton.MiddleButton:
            if self.on_toggle_mark:
                self.on_toggle_mark()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        if self._item.pixmap().isNull():
            return
        if self._fit:
            self.zoom_100(self.mapToScene(event.position().toPoint()))
        else:
            self.fit()
        event.accept()

    # ---- 事件 ----
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.btn_prev.move(14, (self.height() - 48) // 2)
        self.btn_next.move(self.width() - 48 - 14, (self.height() - 48) // 2)
        self.btn_prev.raise_()
        self.btn_next.raise_()
        self._placeholder.setGeometry(self.rect())
        self._placeholder.raise_()
        if self._fit:
            self.fit()

    def viewportEvent(self, event) -> bool:
        t = event.type()
        if t == QEvent.Type.MouseMove:
            self._update_arrows()
        elif t == QEvent.Type.Leave:
            QTimer.singleShot(150, self._update_arrows)
        return super().viewportEvent(event)

    def _update_arrows(self) -> None:
        pos = self.mapFromGlobal(QCursor.pos())
        w = self.width()
        in_y = 0 <= pos.y() <= self.height()
        over_prev_btn = self.btn_prev.geometry().contains(pos)
        over_next_btn = self.btn_next.geometry().contains(pos)
        show_prev = (0 <= pos.x() < w * 0.25 and in_y) or over_prev_btn
        show_next = (w * 0.75 < pos.x() <= w and in_y) or over_next_btn
        has_img = not self._item.pixmap().isNull()
        self.btn_prev.setVisible(show_prev and has_img)
        self.btn_next.setVisible(show_next and has_img)

    def contextMenuEvent(self, event) -> None:
        if self.menu_builder is not None:
            menu = self.menu_builder()
            if menu is not None:
                menu.exec(event.globalPos())
                return
        super().contextMenuEvent(event)


class MainWindow(QMainWindow):
    def __init__(self, config: Config):
        super().__init__()
        self.cfg = config
        self.setWindowTitle("照片筛选工具 PhotoCuller")
        self.resize(1280, 800)

        self.groups: list[PhotoGroup] = []
        self.visible: list[int] = []              # 通过筛选的组下标（指向 self.groups）
        self.current = -1
        self.mode = ReviewMode.NORMAL
        self.undo_stack = UndoStack()
        self.root_dir: Path | None = None
        self._pending_delete: dict[str, int] = {}
        self._full_requested: str | None = None   # 已请求全尺寸解码的路径
        self.filter_days: set[str] = set()        # 空 = 全部日期；否则选中的拍摄日
        self.filter_ratings: set[int] = set()      # 空 = 全部评分；0=未评分, 1-5=对应星级

        # 核心组件
        budget = int(self.cfg.get("cache_mb", 256)) * 1024 * 1024
        self.cache = ImageCache(budget)
        self.prefetcher = Prefetcher(self.cache)
        self.prefetcher.image_ready.connect(self._on_prefetch_ready)
        self.prefetcher.start()

        self.worker = FileOpWorker()
        self.worker.file_started.connect(self._on_file_started)
        self.worker.group_progress.connect(self._on_group_progress)
        self.worker.group_deleted.connect(self._on_group_deleted)
        self.worker.group_failed.connect(self._on_group_failed)
        self.worker.group_restored.connect(self._on_group_restored)
        self.worker.restore_conflict.connect(self._on_restore_conflict)
        self.worker.mark_done.connect(self._on_mark_done)
        self.worker.start()

        self._build_ui()
        QTimer.singleShot(100, self._pick_directory)

    # ---------------- UI 构建 ----------------
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        vbox = QVBoxLayout(central)
        vbox.setContentsMargins(6, 6, 6, 6)

        # 工具栏
        bar = QHBoxLayout()
        self.btn_dir = QPushButton("选择目录")
        self.lbl_dir = QLabel("（未选择）")
        self.lbl_progress = QLabel("")
        self.btn_mode = QPushButton("模式: 删除不满意")
        self.btn_orphan = QPushButton("清理无JPG的RAW")
        self.btn_trash = QPushButton("回收站管理")
        for b in (self.btn_dir, self.btn_mode, self.btn_orphan, self.btn_trash):
            b.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        bar.addWidget(self.btn_dir)
        bar.addWidget(self.lbl_dir, 1)
        bar.addWidget(self.lbl_progress)
        bar.addWidget(self.btn_orphan)
        bar.addWidget(self.btn_mode)
        bar.addWidget(self.btn_trash)
        vbox.addLayout(bar)

        # 筛选与导出目录行
        bar2 = QHBoxLayout()
        lbl_filter = QLabel("筛选:")
        self.btn_date = QToolButton()
        self.btn_date.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_date.setToolTip("按拍摄日期（EXIF，缺则用文件时间）多选，可勾选多天")
        self.menu_date = CheckableMenu(self)
        self.btn_date.setMenu(self.menu_date)
        self.btn_rating = QToolButton()
        self.btn_rating.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.btn_rating.setToolTip("按相机星级评分多选（读取文件 XMP 元数据）")
        self.menu_rating = CheckableMenu(self)
        self.btn_rating.setMenu(self.menu_rating)
        self.lbl_export = QLabel("")
        self.lbl_export.setStyleSheet("color: #888;")
        self.btn_export_dir = QPushButton("设置导出目录")
        self.btn_export_sel = QPushButton("导出筛选结果")
        for w in (self.btn_date, self.btn_rating, self.btn_export_dir,
                  self.btn_export_sel):
            w.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.btn_date.setMinimumWidth(140)
        self.btn_rating.setMinimumWidth(120)
        bar2.addWidget(lbl_filter)
        bar2.addWidget(self.btn_date)
        bar2.addWidget(self.btn_rating)
        bar2.addSpacing(12)
        bar2.addWidget(self.btn_export_dir)
        bar2.addWidget(self.lbl_export, 1)
        bar2.addWidget(self.btn_export_sel)
        vbox.addLayout(bar2)

        self.btn_export_dir.clicked.connect(self._set_export_dir)
        self.btn_export_sel.clicked.connect(self._export_filtered)

        # 中部：图片区 + 右侧胶片栏
        mid = QHBoxLayout()
        self.image = ImageView()
        self.image.on_prev = lambda: self._nav(-1)
        self.image.on_next = lambda: self._nav(1)
        self.image.on_toggle_mark = self._toggle_keep
        self.image.on_need_full = self._request_full_res
        self.image.menu_builder = self._build_image_menu
        mid.addWidget(self.image, 1)

        self.strip = QListWidget()
        self.strip.setFixedWidth(160)
        self.strip.setIconSize(QSize(128, 96))
        self.strip.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.strip.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.strip.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.strip.setStyleSheet(
            "QListWidget { background: #202020; color: #ccc; border: none; }"
            "QListWidget::item { padding: 4px; }"
            "QListWidget::item:selected { background: #3a5a8c; }")
        self.strip.itemClicked.connect(self._on_strip_clicked)
        mid.addWidget(self.strip)
        vbox.addLayout(mid, 1)

        # 组信息条
        self.lbl_info = QLabel("")
        self.lbl_info.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lbl_info.setStyleSheet("padding: 6px; font-size: 14px;")
        self.lbl_info.setTextFormat(Qt.TextFormat.RichText)
        vbox.addWidget(self.lbl_info)

        # 状态栏
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.statusBar().addPermanentWidget(self.progress, 1)
        self.statusBar().showMessage(
            "←/→/滚轮 浏览｜Delete 移走｜Ctrl+Z 撤销｜Space/中键 标记导出｜"
            "Ctrl+E 导出筛选结果｜Ctrl+滚轮/双击 缩放｜Ctrl+C 复制｜O 外部打开")

        self.btn_dir.clicked.connect(self._pick_directory)
        self.btn_mode.clicked.connect(self._toggle_mode)
        self.btn_orphan.clicked.connect(self._cleanup_orphans)
        self.btn_trash.clicked.connect(self._open_trash_window)
        self._rebuild_filter_options()
        self._update_export_label()

    # ---------------- 目录与扫描 ----------------
    def _pick_directory(self) -> None:
        start = self.cfg.get("last_dir") or ""
        d = QFileDialog.getExistingDirectory(self, "选择照片目录（SD卡）", start)
        if not d:
            if not self.groups:
                self.lbl_dir.setText("（未选择目录，点击左上角「选择目录」）")
            return
        self.cfg.set("last_dir", d)
        self.root_dir = Path(d)
        self.rescan()

    def rescan(self) -> None:
        if not self.root_dir:
            return
        # 扫描需读取每张照片的拍摄时间/评分，目录大时略慢，给出等待光标
        QGuiApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            self.cache.clear()
            self.groups = scan_directory(self.root_dir, self.cfg.preview_exts,
                                         self.cfg.sidecar_exts)
        finally:
            QGuiApplication.restoreOverrideCursor()
        self.undo_stack = UndoStack()
        self._pending_delete.clear()
        self._full_requested = None
        self.lbl_dir.setText(str(self.root_dir))
        self._rebuild_filter_options()
        self.visible = self._visible_indexes()
        self._rebuild_strip()
        if not self.visible:
            self.current = -1
            self.image.show_image(
                None, "该目录下没有找到照片" if not self.groups
                else "当前筛选条件下没有照片")
            self.lbl_info.setText("")
            self._update_progress_label()
            return
        self.current = self.visible[0]
        self._show_current()
        self._update_progress_label()
        self.image.setFocus()

    # ---------------- 筛选（日期 / 评分 多选） ----------------
    def _rebuild_filter_options(self) -> None:
        """按当前照片重建两个多选菜单；保留仍然有效的已选条件。"""
        days: dict[str, int] = {}
        for g in self.groups:
            days[g.day] = days.get(g.day, 0) + 1
        self.filter_days &= set(days)          # 目录变了，丢弃已不存在的日期
        self.filter_ratings &= set(range(6))

        self.menu_date.clear()
        act_all = self.menu_date.addAction("全选")
        act_none = self.menu_date.addAction("清空")
        act_all.triggered.connect(lambda *_: self._set_all_days(True))
        act_none.triggered.connect(lambda *_: self._set_all_days(False))
        if days:
            self.menu_date.addSeparator()
        for day in sorted(days, reverse=True):
            act = self.menu_date.addAction(f"{day}（{days[day]} 组）")
            act.setCheckable(True)
            act.setChecked(day in self.filter_days)
            act.triggered.connect(
                lambda checked, d=day: self._toggle_day(d, checked))

        self.menu_rating.clear()
        act_all = self.menu_rating.addAction("全选")
        act_none = self.menu_rating.addAction("清空")
        act_all.triggered.connect(lambda *_: self._set_all_ratings(True))
        act_none.triggered.connect(lambda *_: self._set_all_ratings(False))
        self.menu_rating.addSeparator()
        for value, label in RATING_OPTIONS:
            act = self.menu_rating.addAction(label)
            act.setCheckable(True)
            act.setChecked(value in self.filter_ratings)
            act.triggered.connect(
                lambda checked, v=value: self._toggle_rating(v, checked))

        self._update_filter_buttons()

    def _visible_indexes(self) -> list[int]:
        return filters.visible_indexes(self.groups, self.filter_days,
                                       self.filter_ratings)

    def _toggle_day(self, day: str, checked: bool) -> None:
        if checked:
            self.filter_days.add(day)
        else:
            self.filter_days.discard(day)
        self._after_filter_change()

    def _set_all_days(self, checked: bool) -> None:
        self.filter_days = {g.day for g in self.groups} if checked else set()
        for act in self.menu_date.actions():
            if act.isCheckable():
                act.setChecked(checked)
        self._after_filter_change()

    def _toggle_rating(self, value: int, checked: bool) -> None:
        if checked:
            self.filter_ratings.add(value)
        else:
            self.filter_ratings.discard(value)
        self._after_filter_change()

    def _set_all_ratings(self, checked: bool) -> None:
        self.filter_ratings = set(range(6)) if checked else set()
        for act in self.menu_rating.actions():
            if act.isCheckable():
                act.setChecked(checked)
        self._after_filter_change()

    def _after_filter_change(self) -> None:
        self._update_filter_buttons()
        self._apply_filter()

    def _update_filter_buttons(self) -> None:
        days = self.filter_days
        all_days = {g.day for g in self.groups}
        if not days or days == all_days:          # 全选等同不筛选
            self.btn_date.setText("日期: 全部")
            self.btn_date.setToolTip("已选日期:（全部）")
        elif len(days) == 1:
            only = next(iter(days))
            self.btn_date.setText(f"日期: {only}")
            self.btn_date.setToolTip(f"已选日期: {only}")
        else:
            self.btn_date.setText(f"日期: {len(days)} 天")
            self.btn_date.setToolTip("已选日期: " + "、".join(sorted(days)))

        ratings = self.filter_ratings
        if not ratings or ratings == {v for v, _ in RATING_OPTIONS}:
            self.btn_rating.setText("评分: 全部")
            self.btn_rating.setToolTip("已选评分:（全部）")
            return
        labels = [label for value, label in RATING_OPTIONS if value in ratings]
        if len(labels) <= 2:
            self.btn_rating.setText("评分: " + " ".join(labels))
        else:
            self.btn_rating.setText(f"评分: {len(labels)} 项")
        self.btn_rating.setToolTip("已选评分: " + "、".join(labels))

    def _apply_filter(self) -> None:
        self.visible = self._visible_indexes()
        self._rebuild_strip()
        alive = self._alive_indexes()
        if not alive:
            self.current = -1
            self.image.show_image(
                None, "当前筛选条件下没有照片" if self.visible else "没有可处理的照片")
            self.lbl_info.setText("")
        else:
            if self.current not in alive:
                self.current = alive[0]
            self._show_current()
        self._update_progress_label()
        self.image.setFocus()

    # ---------------- 胶片栏 ----------------
    @staticmethod
    def _strip_label(g: PhotoGroup) -> str:
        lines = [g.basename]
        if g.rating > 0:
            lines.append("★" * g.rating)
        if g.state == GroupState.DELETED:
            lines.append("(已移走)")
        elif g.is_orphan:
            lines.append("(无JPG)")
        return "\n".join(lines)

    def _rebuild_strip(self) -> None:
        self.strip.clear()
        for i in self.visible:
            g = self.groups[i]
            item = QListWidgetItem(self._strip_label(g))
            item.setData(Qt.ItemDataRole.UserRole, i)
            self.strip.addItem(item)
        self._refresh_strip_states()

    def _refresh_strip_states(self) -> None:
        for row in range(self.strip.count()):
            item = self.strip.item(row)
            idx = item.data(Qt.ItemDataRole.UserRole)
            g = self.groups[idx]
            item.setText(self._strip_label(g))
            if g.state == GroupState.DELETED:
                item.setForeground(QColor("#555"))
            elif g.state == GroupState.KEEP:
                item.setForeground(QColor("#7fc97f"))
            elif g.is_orphan:
                item.setForeground(QColor("#777"))
            elif g.rating > 0:
                item.setForeground(QColor("#e0c060"))
            else:
                item.setForeground(QColor("#ccc"))

    def _on_strip_clicked(self, item: QListWidgetItem) -> None:
        idx = item.data(Qt.ItemDataRole.UserRole)
        if idx is None or not (0 <= idx < len(self.groups)):
            return
        if self.groups[idx].state == GroupState.DELETED:
            return
        self.current = idx
        self._show_current()
        self.image.setFocus()

    def _sync_strip(self) -> None:
        if self.current < 0:
            return
        self.strip.blockSignals(True)
        for row in range(self.strip.count()):
            if self.strip.item(row).data(Qt.ItemDataRole.UserRole) == self.current:
                self.strip.setCurrentRow(row)
                self.strip.scrollToItem(self.strip.item(row),
                                        QListWidget.ScrollHint.PositionAtCenter)
                break
        self.strip.blockSignals(False)

    # ---------------- 浏览 ----------------
    def _alive_indexes(self) -> list[int]:
        return [i for i in self.visible
                if self.groups[i].state != GroupState.DELETED]

    def _display_size(self) -> tuple[int, int]:
        s = self.image.viewport().size()
        return s.width() * 2, s.height() * 2

    def _show_current(self) -> None:
        if not (0 <= self.current < len(self.groups)):
            return
        g = self.groups[self.current]
        self._full_requested = None
        if g.is_orphan:
            self.image.show_image(None, f"⚠ {g.basename} — 只有 RAW 文件，无 JPG 预览")
        else:
            path = str(g.preview.path)
            w, h = self._display_size()
            key = Prefetcher.make_key(path, w, h)
            img = self.cache.get(key)
            if img is None:
                img = decode_image(g.preview.path, self.image.viewport().size() * 2)
                if img is not None:
                    self.cache.put(key, img)
            if img is None:
                self.image.show_image(None, f"⚠ 无法解码: {g.preview.name}")
            else:
                self.image.show_image(img)
        self._update_info_bar()
        self._update_progress_label()
        self._sync_strip()
        self._refresh_strip_states()
        self._request_prefetch()

    def _request_prefetch(self) -> None:
        if not self.groups:
            return
        w, h = self._display_size()
        jobs: list[tuple[str, int, int]] = []
        alive = self._alive_indexes()
        if self.current not in alive:
            return
        pos = alive.index(self.current)
        # 主图预取：相邻 N 组
        n = int(self.cfg.get("prefetch_count", 3))
        for offset in list(range(1, n + 1)) + [-1]:
            if 0 <= pos + offset < len(alive):
                g = self.groups[alive[pos + offset]]
                if g.preview:
                    jobs.append((str(g.preview.path), w, h))
        # 胶片栏缩略图：当前前后各 8 组
        for offset in range(-8, 9):
            if 0 <= pos + offset < len(alive):
                g = self.groups[alive[pos + offset]]
                if g.preview:
                    jobs.append((str(g.preview.path), THUMB_W, THUMB_H))
        self.prefetcher.request(jobs)

    def _on_prefetch_ready(self, key: str) -> None:
        try:
            size_part, path = key.split("|", 1)
            w_s, _ = size_part.split("x", 1)
            w = int(w_s)
        except ValueError:
            return
        # 缩略图就绪 → 更新胶片栏对应项
        if w == THUMB_W:
            img = self.cache.get(key)
            if img is None:
                return
            for row in range(self.strip.count()):
                item = self.strip.item(row)
                idx = item.data(Qt.ItemDataRole.UserRole)
                g = self.groups[idx]
                if g.preview and str(g.preview.path) == path:
                    item.setIcon(QIcon(QPixmap.fromImage(img)))
                    break
            return
        # 全尺寸就绪 → 若是当前图则原位替换
        if w == 0:
            if 0 <= self.current < len(self.groups):
                g = self.groups[self.current]
                if g.preview and str(g.preview.path) == path:
                    img = self.cache.get(key)
                    if img is not None:
                        self.image.replace_keep_view(img)
            return
        # 主图就绪 → 若是当前图则刷新
        if 0 <= self.current < len(self.groups):
            g = self.groups[self.current]
            if g.preview and str(g.preview.path) == path:
                img = self.cache.get(key)
                if img is not None:
                    self.image.show_image(img)

    def _request_full_res(self) -> None:
        """放大时请求当前图的全尺寸解码（后台）。"""
        if not (0 <= self.current < len(self.groups)):
            return
        g = self.groups[self.current]
        if g.preview is None:
            return
        path = str(g.preview.path)
        if self.cache.get(Prefetcher.make_key(path, 0, 0)) is not None:
            return
        if self._full_requested == path:
            return
        self._full_requested = path
        self.prefetcher.request([(path, 0, 0)])

    def _nav(self, step: int) -> None:
        alive = self._alive_indexes()
        if not alive:
            return
        if self.current not in alive:
            nxt = [i for i in alive if i > self.current]
            prv = [i for i in alive if i < self.current]
            if step > 0 and nxt:
                self.current = nxt[0]
            elif prv:
                self.current = prv[-1]
            elif nxt:
                self.current = nxt[0]
            else:
                self.current = alive[0]
        else:
            pos = alive.index(self.current) + step
            pos = max(0, min(pos, len(alive) - 1))
            self.current = alive[pos]
        self._show_current()

    def _nav_edge(self, first: bool) -> None:
        alive = self._alive_indexes()
        if alive:
            self.current = alive[0] if first else alive[-1]
            self._show_current()

    # ---------------- 复制 / 外部打开 ----------------
    def _current_preview_path(self) -> Path | None:
        if 0 <= self.current < len(self.groups):
            g = self.groups[self.current]
            if g.preview:
                return g.preview.path
        return None

    def _copy_current(self) -> None:
        path = self._current_preview_path()
        if path is None:
            return
        key = Prefetcher.make_key(str(path), 0, 0)
        img = self.cache.get(key)
        if img is None:
            img = decode_image(path, self.image.viewport().size() * 2)
        if img is None:
            self.statusBar().showMessage("复制失败：无法解码图片", 3000)
            return
        mime = QMimeData()
        mime.setImageData(img)
        mime.setUrls([QUrl.fromLocalFile(str(path))])
        QGuiApplication.clipboard().setMimeData(mime)
        self.statusBar().showMessage(f"已复制: {path.name}", 2500)

    def _open_external(self, choose: bool = False) -> None:
        path = self._current_preview_path()
        if path is None:
            return
        try:
            if choose:
                subprocess.Popen(["rundll32.exe", "shell32.dll,OpenAs_RunDLL", str(path)])
            else:
                os.startfile(str(path))  # noqa: S606 用系统默认程序打开
        except OSError as e:
            QMessageBox.warning(self, "打开失败", str(e))

    def _build_image_menu(self) -> QMenu | None:
        if self._current_preview_path() is None:
            return None
        menu = QMenu(self)
        menu.addAction("复制图片\tCtrl+C", self._copy_current)
        menu.addSeparator()
        menu.addAction("放大到 100%\t双击", lambda: self.image.zoom_100(
            self.image.mapToScene(self.image.viewport().rect().center())))
        menu.addAction("适应窗口", self.image.fit)
        menu.addSeparator()
        menu.addAction("用默认程序打开\tO", lambda: self._open_external(False))
        menu.addAction("选择打开方式…\tCtrl+O", lambda: self._open_external(True))
        return menu

    # ---------------- 信息条 ----------------
    def _update_info_bar(self) -> None:
        if not (0 <= self.current < len(self.groups)):
            self.lbl_info.setText("")
            return
        g = self.groups[self.current]
        badges = []
        for ext, label in ((".jpg", "JPG"), (".cr3", "CR3"), (".hif", "HIF")):
            if ext in g.exts_present() or (ext == ".jpg" and ".jpeg" in g.exts_present()):
                badges.append(f'<span style="background:#2e7d32;color:#fff;'
                              f'padding:2px 8px;border-radius:3px;">{label} ✓</span>')
            else:
                badges.append(f'<span style="background:#555;color:#aaa;'
                              f'padding:2px 8px;border-radius:3px;">{label} —</span>')
        when = datetime.fromtimestamp(g.effective_time).strftime("%Y-%m-%d %H:%M")
        state_text = {GroupState.PENDING: "", GroupState.KEEP: "｜⭐已标记保留",
                      GroupState.FAILED: "｜❌移动失败"}[g.state]
        orphan = "｜⚠无JPG预览" if g.is_orphan else ""
        if g.rating > 0:
            stars_html = (f"　<span style='color:#e0c060;font-size:15px'>"
                          f"{'★' * g.rating}{'☆' * (5 - g.rating)}</span>")
        else:
            stars_html = "　<span style='color:#555;font-size:15px'>☆☆☆☆☆</span>"
        exposure_html = ""
        if g.preview is not None:
            exp = format_exposure(read_exposure(g.preview.path))
            if exp:
                exposure_html = f"<br><span style='color:#d8a94e'>{exp}</span>"
        pos = self.visible.index(self.current) + 1 if self.current in self.visible else 0
        self.lbl_info.setText(
            f"<b>{g.basename}</b>{stars_html}　{' '.join(badges)}{exposure_html}<br>"
            f"<span style='color:#888'>{human_size(g.total_size)}｜{when}｜"
            f"第 {pos}/{len(self.visible)} 组{state_text}{orphan}</span>")

    def _update_progress_label(self) -> None:
        total = len(self.groups)
        vis = len(self.visible)
        deleted = sum(1 for g in self.groups if g.state == GroupState.DELETED)
        kept = sum(1 for g in self.groups if g.state == GroupState.KEEP)
        scope = (f"筛选 {vis}/{total} 组｜" if vis != total else f"共 {total} 组｜")
        self.lbl_progress.setText(f"{scope}已移走 {deleted}｜保留 {kept}　")

    # ---------------- 删除 / 撤销 / 标记 ----------------
    def _delete_current(self) -> None:
        if not (0 <= self.current < len(self.groups)):
            return
        g = self.groups[self.current]
        if g.state == GroupState.DELETED:
            return
        trash_root = self.cfg.trash_root
        if trash_root is None:
            QMessageBox.warning(self, "未设置回收目录", "请先设置回收目录。")
            return
        if self.cfg.get("confirm_delete"):
            ret = QMessageBox.question(self, "确认", f"移走 {g.basename} 整组？")
            if ret != QMessageBox.StandardButton.Yes:
                return
        alive = self._alive_indexes()
        pos = alive.index(self.current)
        if pos + 1 < len(alive):
            next_idx = alive[pos + 1]
        elif pos > 0:
            next_idx = alive[pos - 1]
        else:
            next_idx = -1

        g.state = GroupState.DELETED
        self._pending_delete[g.basename] = self.current
        self.worker.enqueue_delete(g, trash_root)
        self._update_progress_label()

        if next_idx >= 0:
            self.current = next_idx
            self._show_current()
        else:
            self.current = -1
            self.image.show_image(None, "全部照片都处理完了 🎉")
            self.lbl_info.setText("")

    def _undo(self) -> None:
        cmd = self.undo_stack.pop()
        if cmd is None:
            self.statusBar().showMessage("没有可撤销的操作", 2000)
            return
        self.worker.enqueue_restore(cmd.record)
        self.statusBar().showMessage(f"正在恢复 {cmd.basename} ...", 3000)

    # ---------------- 导出（标记保留）目录 ----------------
    def _export_root(self) -> Path | None:
        """当前导出目录；未设置时回退到旧版默认（回收站同级\标记保留）。"""
        p = self.cfg.export_root
        if p is not None:
            return p
        trash_root = self.cfg.trash_root
        if trash_root is not None:
            return trash_root.parent / "标记保留"
        return None

    def _update_export_label(self) -> None:
        root = self._export_root()
        if root is None:
            self.lbl_export.setText("导出目录: （未设置）")
            self.lbl_export.setToolTip("")
            return
        self.lbl_export.setText(f"导出目录: {root.name or root}")
        self.lbl_export.setToolTip(str(root))

    def _set_export_dir(self) -> None:
        root = self._export_root()
        start = str(root) if root else str(Path.home() / "Desktop")
        d = QFileDialog.getExistingDirectory(
            self, "选择导出（标记保留）目录（可在对话框内新建文件夹）", start,
            QFileDialog.Option.ShowDirsOnly)
        if not d:
            return
        self.cfg.set("export_root", d)
        self._update_export_label()
        self.statusBar().showMessage(f"导出目录已设为: {d}", 4000)

    def _confirm_targets(self, title: str, intro_html: str,
                         targets: list[PhotoGroup], ok_text: str) -> bool:
        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        dlg.resize(520, 420)
        v = QVBoxLayout(dlg)
        intro = QLabel(intro_html)
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setWordWrap(True)
        v.addWidget(intro)
        lst = QListWidget()
        for g in targets:
            exts = "/".join(sorted(g.exts_present()))
            stars = ("★" * g.rating) if g.rating else ""
            lst.addItem(f"{g.basename}  [{exts}]  {stars}  {human_size(g.total_size)}")
        v.addWidget(lst)
        btns = QHBoxLayout()
        ok = QPushButton(ok_text)
        cancel = QPushButton("取消")
        btns.addStretch(1)
        btns.addWidget(ok)
        btns.addWidget(cancel)
        v.addLayout(btns)
        ok.clicked.connect(dlg.accept)
        cancel.clicked.connect(dlg.reject)
        return dlg.exec() == QDialog.DialogCode.Accepted

    def _export_filtered(self) -> None:
        """把当前筛选出的待处理组一次性复制到导出目录并标记保留。"""
        root = self._export_root()
        if root is None:
            QMessageBox.warning(self, "未设置导出目录",
                                "请先点击「设置导出目录」指定导出位置。")
            return
        targets = [self.groups[i] for i in self.visible
                   if self.groups[i].state == GroupState.PENDING]
        if not targets:
            QMessageBox.information(
                self, "导出筛选结果",
                "当前筛选结果里没有待导出的照片组（可能都已导出或已移走）。")
            return
        total_size = sum(g.total_size for g in targets)
        if not self._confirm_targets(
                "导出筛选结果",
                f"将把 <b>{len(targets)}</b> 组（共 <b>{human_size(total_size)}</b>）"
                f"复制到：<br><b>{root}</b><br>并标记为保留。确认导出？",
                targets, "确认导出"):
            return
        for g in targets:
            g.state = GroupState.KEEP
            self.worker.enqueue_mark(g, root, True)
        self._update_progress_label()
        self._refresh_strip_states()
        self.statusBar().showMessage(
            f"正在导出 {len(targets)} 组到 {root} ...", 5000)

    def _toggle_keep(self) -> None:
        if not (0 <= self.current < len(self.groups)):
            return
        g = self.groups[self.current]
        if g.state == GroupState.DELETED:
            return
        g.state = GroupState.PENDING if g.state == GroupState.KEEP else GroupState.KEEP
        mark_root = self._export_root()
        if mark_root is not None:
            self.worker.enqueue_mark(g, mark_root, g.state == GroupState.KEEP)
        self._update_info_bar()
        self._update_progress_label()
        self._refresh_strip_states()
        if g.state == GroupState.KEEP:
            self._nav(1)

    def _on_mark_done(self, basename: str, marked: bool, msg: str) -> None:
        mark_root = self._export_root()
        if msg:
            self.statusBar().showMessage(f"{basename}: {msg}", 4000)
        elif marked:
            self.statusBar().showMessage(
                f"已标记 {basename}，副本在: {mark_root}", 4000)
        else:
            self.statusBar().showMessage(f"已取消标记 {basename}", 3000)

    def _toggle_mode(self) -> None:
        self.mode = (ReviewMode.MARK_KEEP if self.mode == ReviewMode.NORMAL
                     else ReviewMode.NORMAL)
        self.btn_mode.setText("模式: 标记保留(反选)" if self.mode == ReviewMode.MARK_KEEP
                              else "模式: 删除不满意")
        hint = ("反选模式: Space/中键 标记想保留的，Ctrl+Enter 批量移走其余"
                if self.mode == ReviewMode.MARK_KEEP else
                "←/→/滚轮 浏览｜Delete 移走｜Ctrl+Z 撤销｜Space/中键 标记")
        self.statusBar().showMessage(hint, 5000)

    def _batch_delete_unmarked(self) -> None:
        targets = [self.groups[i] for i in self.visible
                   if self.groups[i].state == GroupState.PENDING]
        if not targets:
            QMessageBox.information(self, "批量移走", "没有未标记的照片组。")
            return
        total_size = sum(g.total_size for g in targets)
        if not self._confirm_targets(
                "确认批量移走",
                f"将移走 <b>{len(targets)}</b> 组，共 <b>{human_size(total_size)}</b>"
                "（含同名 RAW 文件）：", targets, "确认移走"):
            return

        trash_root = self.cfg.trash_root
        for g in targets:
            g.state = GroupState.DELETED
            self._pending_delete[g.basename] = self.groups.index(g)
            self.worker.enqueue_delete(g, trash_root)
        self._update_progress_label()
        self._refresh_strip_states()
        self._show_current()

    def _cleanup_orphans(self) -> None:
        """一键移走当前范围内所有没有对应 JPG 的 RAW 组（孤儿组）。"""
        targets = [self.groups[i] for i in self.visible
                   if self.groups[i].is_orphan
                   and self.groups[i].state == GroupState.PENDING]
        if not targets:
            QMessageBox.information(self, "清理无JPG的RAW",
                                    "当前范围内没有缺少 JPG 配套的 RAW 文件，已经很干净了。")
            return
        total_size = sum(g.total_size for g in targets)
        if not self._confirm_targets(
                "清理无JPG的RAW",
                f"发现 <b>{len(targets)}</b> 个没有 JPG 配套的 RAW 文件，"
                f"共 <b>{human_size(total_size)}</b>。<br>"
                "移走后可在回收站管理中恢复。确认清理以下文件？",
                targets, "确认清理"):
            return

        trash_root = self.cfg.trash_root
        if trash_root is None:
            return
        for g in targets:
            g.state = GroupState.DELETED
            self._pending_delete[g.basename] = self.groups.index(g)
            self.worker.enqueue_delete(g, trash_root)
        self._update_progress_label()
        self._refresh_strip_states()
        alive = self._alive_indexes()
        if alive:
            self.current = alive[0] if self.current not in alive else self.current
            self._show_current()
        self.statusBar().showMessage(
            f"正在清理 {len(targets)} 个孤儿RAW文件...", 4000)

    # ---------------- 工作线程信号 ----------------
    def _on_file_started(self, name: str, size: int) -> None:
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.statusBar().showMessage(f"正在移动并校验: {name} ({human_size(size)})")

    def _on_group_progress(self, done: int, total: int) -> None:
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(done)

    def _on_group_deleted(self, basename: str, record: dict) -> None:
        idx = self._pending_delete.pop(basename, -1)
        if idx >= 0:
            self.undo_stack.push(DeleteCommand(basename, record, idx))
        self.progress.setVisible(False)
        self.statusBar().showMessage(
            f"已移走 {basename}（{len(record.get('files', []))} 个文件，CRC32 校验通过）", 4000)
        self._update_progress_label()
        self._update_info_bar()
        self._refresh_strip_states()

    def _on_group_failed(self, basename: str, msg: str) -> None:
        self._pending_delete.pop(basename, None)
        for g in self.groups:
            if g.basename == basename:
                g.state = GroupState.FAILED
                break
        self.progress.setVisible(False)
        QMessageBox.warning(self, "移动失败",
                            f"{basename} 移动失败（源文件已保留）：\n{msg}")
        self._update_progress_label()
        self._update_info_bar()

    def _on_group_restored(self, record_id: str, conflicts: list) -> None:
        self.rescan()
        msg = "恢复完成" + (f"，{len(conflicts)} 个文件冲突被跳过/改名" if conflicts else "")
        self.statusBar().showMessage(msg, 4000)

    def _on_restore_conflict(self, filename: str) -> None:
        box = QMessageBox(self)
        box.setWindowTitle("恢复冲突")
        box.setText(f"原位置已存在同名文件:\n{filename}\n\n如何处理？")
        btn_skip = box.addButton("跳过该文件", QMessageBox.ButtonRole.RejectRole)
        btn_rename = box.addButton("自动重命名", QMessageBox.ButtonRole.ActionRole)
        btn_over = box.addButton("覆盖", QMessageBox.ButtonRole.DestructiveRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked is btn_rename:
            self.worker.answer_conflict("rename")
        elif clicked is btn_over:
            self.worker.answer_conflict("overwrite")
        else:
            self.worker.answer_conflict("skip")

    # ---------------- 回收站窗口 ----------------
    def _open_trash_window(self) -> None:
        trash_root = self.cfg.trash_root
        if trash_root is None:
            QMessageBox.warning(self, "未设置", "尚未设置回收目录。")
            return
        win = TrashWindow(trash_root, self.worker, self)
        win.exec()
        self.rescan()

    # ---------------- 键盘 ----------------
    def keyPressEvent(self, e: QKeyEvent) -> None:
        key = e.key()
        mods = e.modifiers()
        ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)

        if key == Qt.Key.Key_Left:
            self._nav(-1)
        elif key == Qt.Key.Key_Right:
            self._nav(1)
        elif key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self._delete_current()
        elif key == Qt.Key.Key_Z and ctrl:
            self._undo()
        elif key == Qt.Key.Key_C and ctrl:
            self._copy_current()
        elif key == Qt.Key.Key_E and ctrl:
            self._export_filtered()
        elif key == Qt.Key.Key_O and ctrl:
            self._open_external(True)
        elif key == Qt.Key.Key_O:
            self._open_external(False)
        elif key == Qt.Key.Key_Space:
            self._toggle_keep()
        elif key == Qt.Key.Key_M:
            self._toggle_mode()
        elif key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and ctrl:
            self._batch_delete_unmarked()
        elif key == Qt.Key.Key_Home:
            self._nav_edge(True)
        elif key == Qt.Key.Key_End:
            self._nav_edge(False)
        elif key == Qt.Key.Key_F5 or (key == Qt.Key.Key_R and ctrl):
            self.rescan()
        elif key == Qt.Key.Key_T and ctrl:
            self._open_trash_window()
        else:
            super().keyPressEvent(e)

    # ---------------- 关闭 ----------------
    def closeEvent(self, event) -> None:
        self.prefetcher.stop()
        self.prefetcher.wait(1000)
        self.worker.stop()
        self.worker.wait(2000)
        super().closeEvent(event)
