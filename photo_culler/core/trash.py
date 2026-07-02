"""回收站管理：回收目录结构、manifest.jsonl 追加式日志、历史查询"""
from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path

MANIFEST_NAME = "manifest.jsonl"


def manifest_path(trash_root: Path) -> Path:
    return trash_root / MANIFEST_NAME


def make_trash_dir(trash_root: Path, basename: str) -> Path:
    """为一次删除创建独立子目录：日期/时间戳_basename，天然避免命名冲突。"""
    now = datetime.now()
    day_dir = trash_root / now.strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)
    sub = day_dir / f"{now.strftime('%H%M%S')}_{basename}"
    # 同一秒内同 basename 的极端情况
    n = 1
    candidate = sub
    while candidate.exists():
        n += 1
        candidate = day_dir / f"{now.strftime('%H%M%S')}_{basename}_{n}"
    candidate.mkdir(parents=True)
    return candidate


def unique_target(dir_path: Path, filename: str) -> Path:
    """平铺布局下返回不冲突的目标路径：重名时加 _1/_2 序号。"""
    target = dir_path / filename
    if not target.exists():
        return target
    p = Path(filename)
    n = 1
    while True:
        candidate = dir_path / f"{p.stem}_{n}{p.suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def append_record(trash_root: Path, record: dict) -> None:
    """追加一条 JSONL 记录，崩溃不损坏历史。"""
    mp = manifest_path(trash_root)
    mp.parent.mkdir(parents=True, exist_ok=True)
    with open(mp, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def new_delete_record(src_dir: Path, basename: str, trash_dir: Path,
                      files: list[dict], partial: bool = False,
                      trash_root: Path | None = None) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "op": "delete",
        "time": datetime.now().isoformat(timespec="seconds"),
        "src_dir": str(src_dir),
        "basename": basename,
        "trash_dir": str(trash_dir),
        # 平铺布局后 trash_dir == trash_root；显式存一份便于恢复时定位 manifest
        "trash_root": str(trash_root) if trash_root else None,
        "files": files,          # [{"name":..., "size":..., "src":...}]，name 为回收站内实际文件名
        "partial": partial,
        "restored": False,
    }


def new_restore_record(delete_id: str) -> dict:
    return {
        "id": uuid.uuid4().hex,
        "op": "restore",
        "time": datetime.now().isoformat(timespec="seconds"),
        "restore_of": delete_id,
    }


def load_history(trash_root: Path) -> list[dict]:
    """读取全部删除记录，并标注哪些已被恢复。坏行跳过。"""
    mp = manifest_path(trash_root)
    records: list[dict] = []
    if not mp.exists():
        return records
    restored_ids: set[str] = set()
    try:
        with open(mp, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("op") == "restore":
                    rid = rec.get("restore_of")
                    if rid:
                        restored_ids.add(rid)
                elif rec.get("op") == "delete":
                    records.append(rec)
    except OSError:
        return records
    for rec in records:
        if rec["id"] in restored_ids:
            rec["restored"] = True
    records.sort(key=lambda r: r.get("time", ""), reverse=True)
    return records


def record_disk_status(rec: dict) -> str:
    """根据磁盘实际状态返回: 'in_trash' | 'restored' | 'missing'"""
    if rec.get("restored"):
        return "restored"
    trash_dir = Path(rec.get("trash_dir", ""))
    if not trash_dir.exists():
        return "missing"
    # 平铺布局下 trash_dir 恒为回收根目录，需进一步检查组内文件是否还在
    files = rec.get("files", [])
    if files and not any((trash_dir / f.get("name", "")).exists() for f in files):
        return "missing"
    return "in_trash"
