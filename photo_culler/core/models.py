"""数据模型与状态定义"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


class GroupState(enum.Enum):
    PENDING = "pending"    # 未审核
    KEEP = "keep"          # 标记保留
    DELETED = "deleted"    # 已移入回收目录
    FAILED = "failed"      # 移动失败（可重试）


class ReviewMode(enum.Enum):
    NORMAL = "normal"        # 删不满意的
    MARK_KEEP = "mark_keep"  # 反选：标记保留，其余批量移走


@dataclass
class PhotoFile:
    path: Path
    ext: str          # '.jpg' / '.cr3' 等，小写
    size: int
    mtime: float
    role: str         # 'preview' | 'sidecar'

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class PhotoGroup:
    basename: str                     # 'IMG_0123'
    files: list[PhotoFile] = field(default_factory=list)
    preview: PhotoFile | None = None  # JPG 预览；None = 孤儿组
    latest_mtime: float = 0.0
    capture_time: float = 0.0         # 拍摄时间（EXIF）；0 = 未知（回退 latest_mtime）
    rating: int = 0                   # 相机星级评分 0-5
    state: GroupState = GroupState.PENDING

    @property
    def total_size(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def is_orphan(self) -> bool:
        return self.preview is None

    @property
    def effective_time(self) -> float:
        return self.capture_time or self.latest_mtime

    @property
    def day(self) -> str:
        """拍摄日，'YYYY-MM-DD'，作为日期筛选粒度。"""
        return datetime.fromtimestamp(self.effective_time).strftime("%Y-%m-%d")

    def exts_present(self) -> set[str]:
        return {f.ext for f in self.files}


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{n}B"
        n /= 1024
    return f"{n:.1f}GB"
