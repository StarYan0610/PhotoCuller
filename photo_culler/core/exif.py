"""读取 JPG 的曝光信息（快门/光圈/焦段/ISO），带缓存"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ExifTags

_CACHE: dict[str, dict] = {}
_CACHE_LIMIT = 2000

_TAG_EXPOSURE_TIME = 33434
_TAG_FNUMBER = 33437
_TAG_ISO = 34855
_TAG_FOCAL = 37386


def _to_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def read_exposure(path: Path) -> dict:
    """返回 {'shutter': str|None, 'aperture': ..., 'focal': ..., 'iso': ...}"""
    key = str(path)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    info = {"shutter": None, "aperture": None, "focal": None, "iso": None}
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            sub = exif.get_ifd(ExifTags.IFD.Exif)

        et = _to_float(sub.get(_TAG_EXPOSURE_TIME))
        if et is not None:
            info["shutter"] = f"{et:.1f}s" if et >= 1 else f"1/{round(1 / et)}s"

        fn = _to_float(sub.get(_TAG_FNUMBER))
        if fn is not None:
            info["aperture"] = f"f/{fn:g}"

        iso = sub.get(_TAG_ISO)
        if iso is not None:
            info["iso"] = f"ISO {iso}"

        fl = _to_float(sub.get(_TAG_FOCAL))
        if fl is not None:
            info["focal"] = f"{fl:g}mm"
    except Exception:
        pass  # 解码失败/无EXIF → 全 None

    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[key] = info
    return info


def format_exposure(info: dict) -> str:
    parts = [v for v in (info.get("shutter"), info.get("aperture"),
                         info.get("focal"), info.get("iso")) if v]
    return " ｜ ".join(parts)
