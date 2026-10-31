"""目录扫描与分组：同 basename 的多格式归为一组，按修改时间倒序"""
from __future__ import annotations

import os
from pathlib import Path

from .metadata import read_group_metadata
from .models import PhotoFile, PhotoGroup


def scan_directory(root: Path, preview_exts: list[str], sidecar_exts: list[str]) -> list[PhotoGroup]:
    """扫描单层目录，返回按组内最新 mtime 倒序的组列表（孤儿组排最后）。"""
    preview_set = {e.lower() for e in preview_exts}
    sidecar_set = {e.lower() for e in sidecar_exts}
    known = preview_set | sidecar_set

    groups: dict[str, PhotoGroup] = {}

    try:
        entries = list(os.scandir(root))
    except OSError:
        return []

    for entry in entries:
        if not entry.is_file():
            continue
        name = entry.name
        dot = name.rfind(".")
        if dot <= 0:
            continue
        ext = name[dot:].lower()
        if ext not in known:
            continue
        basename = name[:dot]
        try:
            st = entry.stat()
        except OSError:
            continue

        role = "preview" if ext in preview_set else "sidecar"
        pf = PhotoFile(path=Path(entry.path), ext=ext, size=st.st_size,
                       mtime=st.st_mtime, role=role)

        g = groups.get(basename)
        if g is None:
            g = PhotoGroup(basename=basename)
            groups[basename] = g
        g.files.append(pf)
        if role == "preview" and g.preview is None:
            g.preview = pf
        if pf.mtime > g.latest_mtime:
            g.latest_mtime = pf.mtime

    # 组内文件排序：preview 在前，其余按扩展名
    for g in groups.values():
        g.files.sort(key=lambda f: (f.role != "preview", f.ext))
        g.capture_time, g.rating = read_group_metadata(g)

    normal = sorted((g for g in groups.values() if not g.is_orphan),
                    key=lambda g: g.latest_mtime, reverse=True)
    orphans = sorted((g for g in groups.values() if g.is_orphan),
                     key=lambda g: g.latest_mtime, reverse=True)
    return normal + orphans
