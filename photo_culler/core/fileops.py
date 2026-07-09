"""跨盘移动工作线程：先拷后删 + CRC32 全量校验，任务串行队列"""
from __future__ import annotations

import os
import queue
import re
import shutil
import stat
import time
import zlib
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from . import trash as trashmod
from .models import PhotoGroup

_BUF = 1024 * 1024  # 1MB


def crc32_of(path: Path, cancel=lambda: False) -> int:
    crc = 0
    with open(path, "rb") as f:
        while True:
            if cancel():
                raise InterruptedError("cancelled")
            chunk = f.read(_BUF)
            if not chunk:
                break
            crc = zlib.crc32(chunk, crc)
    return crc & 0xFFFFFFFF


def _force_writable(path: Path) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass


class FileOpWorker(QThread):
    """串行执行移动/恢复任务的工作线程。

    任务类型:
      {"kind": "delete", "group": PhotoGroup, "trash_root": Path}
      {"kind": "restore", "record": dict, "on_conflict": callable}
    """
    # file_started(文件名, 文件字节数)
    file_started = Signal(str, int)
    # group_progress(累计已拷字节, 组总字节)
    group_progress = Signal(int, int)
    # group_deleted(basename, manifest记录dict)
    group_deleted = Signal(str, dict)
    # group_failed(basename, 错误信息)
    group_failed = Signal(str, str)
    # group_restored(record_id, 冲突信息list)
    group_restored = Signal(str, list)
    # restore_conflict(文件名) —— 主线程弹窗后调 answer_conflict 回传
    restore_conflict = Signal(str)
    # mark_done(basename, marked是否已标记, 消息)
    mark_done = Signal(str, bool, str)
    busy_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._q: queue.Queue = queue.Queue()
        self._stop = False
        self._conflict_answer: queue.Queue = queue.Queue()

    # ---------- 主线程接口 ----------
    def enqueue_delete(self, group: PhotoGroup, trash_root: Path) -> None:
        self._q.put({"kind": "delete", "group": group, "trash_root": trash_root})

    def enqueue_restore(self, record: dict) -> None:
        self._q.put({"kind": "restore", "record": record})

    def enqueue_mark(self, group: PhotoGroup, mark_root: Path, marked: bool) -> None:
        """marked=True 复制整组到标记目录；False 从标记目录移除。"""
        self._q.put({"kind": "mark", "group": group,
                     "mark_root": mark_root, "marked": marked})

    def answer_conflict(self, choice: str) -> None:
        """choice: 'skip' | 'rename' | 'overwrite'"""
        self._conflict_answer.put(choice)

    def stop(self) -> None:
        self._stop = True
        self._q.put(None)

    # ---------- 线程体 ----------
    def run(self) -> None:
        while not self._stop:
            try:
                task = self._q.get(timeout=0.2)
            except queue.Empty:
                continue
            if task is None:
                break
            self.busy_changed.emit(True)
            try:
                if task["kind"] == "delete":
                    self._do_delete(task["group"], task["trash_root"])
                elif task["kind"] == "restore":
                    self._do_restore(task["record"])
                elif task["kind"] == "mark":
                    self._do_mark(task["group"], task["mark_root"], task["marked"])
            except Exception as e:  # 兜底，绝不炸线程
                name = ""
                if task["kind"] == "delete":
                    name = task["group"].basename
                self.group_failed.emit(name, f"未预期错误: {e}")
            self.busy_changed.emit(False)

    def _do_delete(self, group: PhotoGroup, trash_root: Path) -> None:
        src_dir = group.files[0].path.parent if group.files else Path("")
        total = group.total_size

        # 磁盘空间预检
        try:
            usage = shutil.disk_usage(trash_root)
            if usage.free < total * 1.1:
                self.group_failed.emit(group.basename, "回收目录所在磁盘空间不足")
                return
        except OSError:
            pass

        # 平铺布局：直接放入回收根目录，重名自动加序号
        trash_root.mkdir(parents=True, exist_ok=True)
        trash_dir = trash_root
        copied: list[tuple[Path, Path]] = []   # (dst, src)
        file_records: list[dict] = []
        copied_bytes = 0
        partial = False

        try:
            # 第一阶段：拷贝 + CRC32 校验
            for pf in group.files:
                src = pf.path
                dst = trashmod.unique_target(trash_dir, pf.name)
                self.file_started.emit(pf.name, pf.size)
                self._copy_verified(src, dst)
                copied.append((dst, src))
                file_records.append({"name": dst.name, "size": pf.size, "src": str(src)})
                copied_bytes += pf.size
                self.group_progress.emit(copied_bytes, total)

            # 第二阶段：全部校验通过后删源文件
            for dst, src in copied:
                try:
                    _force_writable(src)
                    os.remove(src)
                except OSError:
                    # 删不掉源文件（卡被拔出/锁定）：回滚——把已拷文件拷回去过于激进，
                    # 保守处理：记录 partial，保留回收目录副本，报告失败
                    partial = True
                    raise RuntimeError(f"源文件删除失败: {src.name}")

            rec = trashmod.new_delete_record(src_dir, group.basename, trash_dir,
                                             file_records, partial=partial,
                                             trash_root=trash_root)
            trashmod.append_record(trash_root, rec)
            self.group_deleted.emit(group.basename, rec)
        except InterruptedError:
            raise
        except Exception as e:
            # 部分拷贝成功也记录，保证可人工找回
            if file_records:
                rec = trashmod.new_delete_record(
                    src_dir, group.basename, trash_dir, file_records, partial=True,
                    trash_root=trash_root)
                try:
                    trashmod.append_record(trash_root, rec)
                except OSError:
                    pass
            self.group_failed.emit(group.basename, str(e))

    def _copy_verified(self, src: Path, dst: Path) -> None:
        """拷贝并 CRC32 校验，失败重拷一次，仍失败则抛异常（源文件保留）。"""
        for attempt in (1, 2):
            with open(src, "rb") as fin, open(dst, "wb") as fout:
                shutil.copyfileobj(fin, fout, _BUF)
            # 先做廉价的 size 比对
            if src.stat().st_size != dst.stat().st_size:
                if attempt == 2:
                    raise RuntimeError(f"大小校验失败: {src.name}")
                continue
            if crc32_of(src) != crc32_of(dst):
                if attempt == 2:
                    raise RuntimeError(f"CRC32 校验失败: {src.name}")
                continue
            # 保留源文件时间戳
            shutil.copystat(src, dst)
            return

    def _do_restore(self, record: dict) -> None:
        trash_dir = Path(record["trash_dir"])
        conflicts: list[str] = []
        ok_any = False
        for finfo in record.get("files", []):
            src_in_trash = trash_dir / finfo["name"]
            dst = Path(finfo["src"])
            if not src_in_trash.exists():
                continue
            if dst.exists():
                # 通知主线程弹窗，等待选择
                choice = self._wait_conflict_choice(finfo["name"])
                if choice == "skip":
                    conflicts.append(finfo["name"])
                    continue
                elif choice == "rename":
                    dst = self._rename_target(dst)
                elif choice == "overwrite":
                    _force_writable(dst)
            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src_in_trash), str(dst))
                ok_any = True
            except OSError as e:
                conflicts.append(f"{finfo['name']} ({e})")
        if ok_any:
            # 定位 manifest 所在回收根目录：新记录显式存了 trash_root；
            # 旧的嵌套布局记录（日期/时间戳_basename）回退到 parents[1]
            if record.get("trash_root"):
                trash_root = Path(record["trash_root"])
            else:
                trash_root = Path(record["trash_dir"]).parents[1]
            try:
                trashmod.append_record(
                    trash_root,
                    trashmod.new_restore_record(record["id"]))
            except OSError:
                pass
            # 清理空的回收子目录（仅旧嵌套布局；平铺布局 trash_dir 即根目录，不删）
            try:
                if trash_dir != trash_root and trash_dir.exists() \
                        and not any(trash_dir.iterdir()):
                    trash_dir.rmdir()
            except OSError:
                pass
        self.group_restored.emit(record["id"], conflicts)

    def _wait_conflict_choice(self, filename: str) -> str:
        # 通过信号让主线程弹窗；主线程调 answer_conflict 回传
        self.restore_conflict.emit(filename)
        try:
            return self._conflict_answer.get(timeout=300)
        except queue.Empty:
            return "skip"

    @staticmethod
    def _rename_target(dst: Path) -> Path:
        stem, suffix = dst.stem, dst.suffix
        n = 1
        while True:
            candidate = dst.with_name(f"{stem}_{n}{suffix}")
            if not candidate.exists():
                return candidate
            n += 1

    # ---------- 标记保留：复制到标记目录 ----------
    def _do_mark(self, group: PhotoGroup, mark_root: Path, marked: bool) -> None:
        try:
            mark_root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.mark_done.emit(group.basename, marked, f"标记目录不可用: {e}")
            return
        if marked:
            try:
                for pf in group.files:
                    if not pf.path.exists():
                        continue
                    dst = mark_root / pf.name
                    if dst.exists():
                        dst = self._rename_target(dst)
                    shutil.copy2(pf.path, dst)
                self.mark_done.emit(group.basename, True, "")
            except OSError as e:
                self.mark_done.emit(group.basename, True, f"复制失败: {e}")
        else:
            # 取消标记：只删该组的精确副本（原名或原名_n），避免误删导出目录中的无关文件
            removed = 0
            for pf in group.files:
                for candidate in self._mark_siblings(mark_root, pf):
                    try:
                        candidate.unlink()
                        removed += 1
                    except OSError:
                        pass
            self.mark_done.emit(group.basename, False,
                                "" if removed else "未找到已标记的副本")

    @staticmethod
    def _mark_siblings(mark_root: Path, pf) -> list[Path]:
        """匹配导出目录内该文件的副本：`<stem><ext>` 或 `<stem>_<n><ext>`。"""
        pattern = re.compile(r"^" + re.escape(pf.path.stem) + r"(_\d+)?$",
                             re.IGNORECASE)
        out: list[Path] = []
        try:
            for cand in mark_root.iterdir():
                if cand.is_file() and cand.suffix.lower() == pf.ext \
                        and pattern.match(cand.stem):
                    out.append(cand)
        except OSError:
            pass
        return out
