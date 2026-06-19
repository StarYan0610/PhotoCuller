"""照片元数据：拍摄时间（EXIF DateTimeOriginal）与相机星级评分（XMP xmp:Rating）。

- JPG/JPEG：Pillow 一次打开即可拿到 EXIF 与 XMP 原始字节。
- CR3/HIF（ISO BMFF 容器）：Pillow 不支持，手动解析 moov 下的 XMP uuid box。
- 结果按 (路径, mtime, 大小) 缓存，重复扫描不重复读盘。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ExifTags

_TAG_DATETIME_ORIGINAL = 0x9003  # Exif 子 IFD：拍摄时间
_TAG_DATETIME = 0x0132           # IFD0：文件修改时间
_TAG_RATING = 0x4746             # IFD0：Windows/EXIF 评分

# Canon 把评分写在 XMP 里：<xmp:Rating>3</xmp:Rating> 或 xmp:Rating="3"
_XMP_RATING_ELEM = re.compile(rb"<(?:\w+:)?Rating>\s*(\d{1,2})\s*</(?:\w+:)?Rating>")
_XMP_RATING_ATTR = re.compile(rb"[\s\"'](?:\w+:)?Rating\s*=\s*[\"'](\d{1,2})[\"']")

# ISO BMFF(CR3/HIF) 中 XMP 元数据所在的 uuid box 标识
_XMP_UUID = bytes.fromhex("be7acfcb97a942e89c71999491e3afac")

_IMAGE_EXTS = {".jpg", ".jpeg", ".tif", ".tiff"}


@dataclass(frozen=True)
class MediaInfo:
    capture_ts: float  # 拍摄时间戳；0 = 未知
    rating: int        # 0-5，0 = 未评分


_CACHE: dict[tuple[str, float, int], MediaInfo] = {}
_CACHE_LIMIT = 6000


def _parse_exif_dt(value) -> float:
    if isinstance(value, bytes):
        value = value.decode("ascii", "ignore")
    if not isinstance(value, str):
        return 0.0
    value = value.strip().rstrip("\x00")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).timestamp()
        except ValueError:
            continue
    return 0.0


def _rating_from_xmp(xmp) -> int:
    if not xmp:
        return 0
    if isinstance(xmp, str):
        xmp = xmp.encode("utf-8", "ignore")
    m = _XMP_RATING_ELEM.search(xmp) or _XMP_RATING_ATTR.search(xmp)
    if m is None:
        return 0
    try:
        n = int(m.group(m.lastindex))
    except (TypeError, ValueError):
        return 0
    return n if 0 <= n <= 5 else 0


def _clamp_rating(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if 0 <= n <= 5 else 0


def _read_image(path: Path) -> MediaInfo:
    ts = 0.0
    rating = 0
    xmp = None
    try:
        with Image.open(path) as im:
            try:
                exif = im.getexif()
                sub = exif.get_ifd(ExifTags.IFD.Exif)
                ts = (_parse_exif_dt(sub.get(_TAG_DATETIME_ORIGINAL))
                      or _parse_exif_dt(exif.get(_TAG_DATETIME)))
                rating = _clamp_rating(exif.get(_TAG_RATING))
            except Exception:
                pass
            xmp = im.info.get("xmp")
    except Exception:
        return MediaInfo(0.0, 0)
    # XMP 评分优先（Canon 只写 XMP，不写 EXIF Rating 标签）
    xmp_rating = _rating_from_xmp(xmp)
    if xmp_rating:
        rating = xmp_rating
    return MediaInfo(ts, rating)


def _iter_boxes(f, end: int):
    """迭代 [当前位置, end) 内的 ISO BMFF box，产出 (type, body_start, box_end)。"""
    while f.tell() + 8 <= end:
        start = f.tell()
        hdr = f.read(8)
        if len(hdr) < 8:
            return
        size = int.from_bytes(hdr[:4], "big")
        typ = hdr[4:8]
        body = start + 8
        if size == 1:  # 64 位长度
            ext = f.read(8)
            if len(ext) < 8:
                return
            size = int.from_bytes(ext, "big")
            body = start + 16
        elif size == 0:  # 延伸至末尾
            size = end - start
        if size < 8 or start + size > end:
            return
        yield typ, body, start + size
        f.seek(start + size)


def _read_bmff(path: Path) -> MediaInfo:
    """解析 CR3/HIF：在 moov/uuid(XMP) 中找 xmp:Rating。"""
    rating = 0
    try:
        with open(path, "rb") as f:
            fsize = f.seek(0, 2)
            f.seek(0)
            moov = None
            for typ, body, bend in _iter_boxes(f, fsize):
                if typ == b"moov":
                    moov = (body, bend)
                    break
            if moov is not None:
                f.seek(moov[0])
                for typ, body, bend in _iter_boxes(f, moov[1]):
                    if typ != b"uuid":
                        continue
                    usertype = f.read(16)
                    if len(usertype) == 16 and usertype == _XMP_UUID:
                        payload = f.read(min(bend - body - 16, 65536))
                        r = _rating_from_xmp(payload)
                        if r:
                            rating = r
                            break
    except Exception:
        return MediaInfo(0.0, 0)
    return MediaInfo(0.0, rating)


def read_media_info(path: Path) -> MediaInfo:
    try:
        st = path.stat()
        key = (str(path), st.st_mtime, st.st_size)
    except OSError:
        return MediaInfo(0.0, 0)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    info = _read_image(path) if path.suffix.lower() in _IMAGE_EXTS else _read_bmff(path)
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[key] = info
    return info


def read_group_metadata(group) -> tuple[float, int]:
    """返回 (拍摄时间戳, 评分)：优先 JPG 预览；评分缺失时回退到同名 RAW/HIF。"""
    ts = 0.0
    rating = 0
    if group.preview is not None:
        info = read_media_info(group.preview.path)
        ts = info.capture_ts
        rating = info.rating
    if rating == 0:
        for pf in group.files:
            if pf.role != "sidecar":
                continue
            info = read_media_info(pf.path)
            if info.rating:
                rating = info.rating
                break
            if ts == 0.0 and info.capture_ts:
                ts = info.capture_ts
    if ts == 0.0:
        ts = group.latest_mtime or 0.0
    return ts, rating
