"""配置读写：%APPDATA%/PhotoCuller/config.json"""
from __future__ import annotations

import json
import os
from pathlib import Path

APP_NAME = "PhotoCuller"

DEFAULT_CONFIG = {
    "preview_exts": [".jpg", ".jpeg"],
    "sidecar_exts": [".cr3", ".hif", ".heif"],
    "trash_root": "",            # 空 = 首次运行时需要用户选择
    "export_root": "",           # 导出（标记保留）目录；空 = 回退到 回收站同级\标记保留
    "last_dir": "",              # 上次浏览的目录
    "cache_mb": 256,
    "prefetch_count": 3,
    "confirm_delete": False,
    "verify_copy": "crc32",
}


def config_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    d = Path(base) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return config_dir() / "config.json"


class Config:
    def __init__(self) -> None:
        self._data = dict(DEFAULT_CONFIG)
        self.load()

    def load(self) -> None:
        p = config_path()
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    stored = json.load(f)
                if not isinstance(stored, dict):
                    return
                for k, v in stored.items():
                    if k in self._data:
                        self._data[k] = v
            except (json.JSONDecodeError, OSError):
                pass  # 配置损坏则用默认值

    def save(self) -> None:
        try:
            with open(config_path(), "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        self._data[key] = value
        self.save()

    @property
    def preview_exts(self) -> list[str]:
        return [e.lower() for e in self._data["preview_exts"]]

    @property
    def sidecar_exts(self) -> list[str]:
        return [e.lower() for e in self._data["sidecar_exts"]]

    @property
    def all_exts(self) -> set[str]:
        return set(self.preview_exts) | set(self.sidecar_exts)

    @property
    def trash_root(self) -> Path | None:
        v = self._data.get("trash_root") or ""
        return Path(v) if v else None

    @property
    def export_root(self) -> Path | None:
        v = self._data.get("export_root") or ""
        return Path(v) if v else None
