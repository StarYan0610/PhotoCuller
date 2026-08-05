"""JPG 解码、EXIF 旋转、屏幕尺寸解码、LRU 缓存与后台预取"""
from __future__ import annotations

import queue
import threading
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QSize, QThread, Signal
from PySide6.QtGui import QImage, QImageIOHandler, QImageReader


def decode_image(path: Path, target_size: QSize) -> QImage | None:
    """按目标尺寸解码（自动处理 EXIF 旋转）。失败返回 None。"""
    reader = QImageReader(str(path))
    reader.setAutoTransform(True)
    if target_size.isValid() and not target_size.isEmpty():
        orig = reader.size()
        if orig.isValid() and not orig.isEmpty():
            # EXIF 旋转 90/270 时宽高互换，按旋转后的方向计算缩放比
            transposed = bool(reader.transformation() & (
                QImageIOHandler.Transformation.TransformationRotate90 |
                QImageIOHandler.Transformation.TransformationRotate270))
            disp_w = orig.height() if transposed else orig.width()
            disp_h = orig.width() if transposed else orig.height()
            if disp_w > target_size.width() or disp_h > target_size.height():
                ratio = min(target_size.width() / disp_w,
                            target_size.height() / disp_h)
                # setScaledSize 作用于旋转前的原始坐标系
                reader.setScaledSize(QSize(max(1, int(orig.width() * ratio)),
                                           max(1, int(orig.height() * ratio))))
    img = reader.read()
    return img if not img.isNull() else None


class ImageCache:
    """按字节预算的 LRU 缓存"""

    def __init__(self, budget_bytes: int = 256 * 1024 * 1024) -> None:
        self._budget = budget_bytes
        self._items: OrderedDict[str, tuple[QImage, int]] = OrderedDict()
        self._used = 0
        self._lock = threading.Lock()

    def get(self, key: str) -> QImage | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            self._items.move_to_end(key)
            return item[0]

    def put(self, key: str, img: QImage) -> None:
        size = img.sizeInBytes()
        with self._lock:
            old = self._items.pop(key, None)
            if old:
                self._used -= old[1]
            while self._used + size > self._budget and self._items:
                _, (_, s) = self._items.popitem(last=False)
                self._used -= s
            self._items[key] = (img, size)
            self._used += size

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._used = 0


class Prefetcher(QThread):
    """单线程后台预取：新请求到来时丢弃过期任务，始终预取最新目标。"""

    image_ready = Signal(str)   # 缓存键 "WxH|path" 解码完成（0x0 = 全尺寸）

    def __init__(self, cache: ImageCache, parent=None) -> None:
        super().__init__(parent)
        self._cache = cache
        self._q: queue.Queue = queue.Queue()
        self._stop = False

    @staticmethod
    def make_key(path: str, w: int, h: int) -> str:
        return f"{w}x{h}|{path}"

    def request(self, jobs: list[tuple[str, int, int]]) -> None:
        """jobs: [(path, w, h), ...] 按优先级从高到低。清空旧队列。
        w=h=0 表示全尺寸解码。"""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        for job in jobs:
            self._q.put(job)

    def stop(self) -> None:
        self._stop = True
        self._q.put(None)

    def run(self) -> None:
        while not self._stop:
            try:
                job = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if job is None:
                break
            path, w, h = job
            key = self.make_key(path, w, h)
            if self._cache.get(key) is not None:
                continue
            img = decode_image(Path(path), QSize(w, h))
            if img is not None:
                self._cache.put(key, img)
                self.image_ready.emit(key)
