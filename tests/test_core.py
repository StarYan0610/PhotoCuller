"""核心逻辑测试：扫描分组、回收站 manifest、移动+CRC32 校验、撤销恢复、
元数据（拍摄时间/星级评分）与筛选条件。
不依赖 Qt 事件循环（FileOpWorker 不启动线程，直接调内部方法）。"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from photo_culler.core.models import GroupState, PhotoGroup, PhotoFile
from photo_culler.core.scanner import scan_directory
from photo_culler.core import filters
from photo_culler.core import metadata as metamod
from photo_culler.core import trash as trashmod


def make_fake_photos(root: Path, names: list[str], with_hif: set[str] | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    with_hif = with_hif or set()
    for name in names:
        (root / f"{name}.JPG").write_bytes(b"\xff\xd8\xff\xe0" + b"x" * 1000)
        (root / f"{name}.CR3").write_bytes(b"\x00" * 2000 + name.encode())
        if name in with_hif:
            (root / f"{name}.HIF").write_bytes(b"\x00" * 500)


def make_real_jpeg(path: Path, rating: int | None = None,
                   dt: str = "2026:09:05 17:50:12") -> None:
    """生成一张真实可解码的 JPEG，写入 EXIF 拍摄时间与 XMP 评分。"""
    from PIL import Image, ExifTags
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (24, 24), (30, 40, 50))
    ex = Image.Exif()
    ex.get_ifd(ExifTags.IFD.Exif)[0x9003] = dt      # DateTimeOriginal
    kwargs: dict = {"exif": ex}
    if rating is not None:
        xmp = (
            '<?xpacket begin="\ufeff"?>'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
            '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
            '<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
            f"<xmp:Rating>{rating}</xmp:Rating>"
            "</rdf:Description></rdf:RDF></x:xmpmeta>"
        ).encode("utf-8")
        kwargs["xmp"] = xmp
    im.save(path, format="JPEG", **kwargs)


def _box(typ: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + typ + payload


def make_fake_cr3(path: Path, rating: int) -> None:
    """构造最小 CR3（ISO BMFF）：ftyp + moov/uuid(XMP)，只用于验证评分解析。"""
    xmp = (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
        f"<xmp:Rating>{rating}</xmp:Rating>"
        "</rdf:Description></rdf:RDF></x:xmpmeta>"
    ).encode("utf-8")
    payload = metamod._XMP_UUID + xmp + b" " * ((-len(xmp)) % 4)
    ftyp = _box(b"ftyp", b"crx " + (0).to_bytes(4, "big") + b"crx ")
    moov = _box(b"moov", _box(b"uuid", payload))
    path.write_bytes(ftyp + moov)


def test_scan_and_group(tmp: Path) -> None:
    src = tmp / "sd" / "DCIM" / "100CANON"
    make_fake_photos(src, ["IMG_0001", "IMG_0002", "IMG_0003"], with_hif={"IMG_0002"})
    # 孤儿 RAW（无 JPG）
    (src / "IMG_0099.CR3").write_bytes(b"\x00" * 100)
    # 无关文件应被忽略
    (src / "readme.txt").write_text("hi")

    groups = scan_directory(src, [".jpg", ".jpeg"], [".cr3", ".hif", ".heif"])
    assert len(groups) == 4, f"期望 4 组，实际 {len(groups)}"
    assert groups[-1].is_orphan and groups[-1].basename == "IMG_0099", "孤儿组应排最后"
    g2 = next(g for g in groups if g.basename == "IMG_0002")
    assert g2.exts_present() == {".jpg", ".cr3", ".hif"}, g2.exts_present()
    assert g2.preview is not None and g2.preview.ext == ".jpg"
    print(f"[OK] 扫描分组: {len(groups)} 组，孤儿组排尾，HIF 识别正常")


def test_delete_and_crc32(tmp: Path) -> None:
    """直接调用 FileOpWorker 内部方法（不进线程）验证 拷贝→CRC32→删源。"""
    from photo_culler.core.fileops import FileOpWorker, crc32_of

    src = tmp / "sd"
    make_fake_photos(src, ["IMG_0001", "IMG_0002"])
    trash_root = tmp / "trash"

    groups = scan_directory(src, [".jpg"], [".cr3"])
    worker = FileOpWorker()
    results: dict = {}
    worker.group_deleted.connect(lambda b, r: results.update(ok=(b, r)))
    worker.group_failed.connect(lambda b, m: results.update(fail=(b, m)))

    worker._do_delete(groups[0], trash_root)
    assert "ok" in results, f"删除应成功: {results}"
    basename, rec = results["ok"]

    # 源文件应已删除
    assert not (src / f"{basename}.JPG").exists()
    assert not (src / f"{basename}.CR3").exists()
    # 回收目录应有整组文件
    tdir = Path(rec["trash_dir"])
    assert (tdir / f"{basename}.JPG").exists()
    assert (tdir / f"{basename}.CR3").exists()
    # manifest 应有记录
    history = trashmod.load_history(trash_root)
    assert len(history) == 1 and history[0]["basename"] == basename
    assert trashmod.record_disk_status(history[0]) == "in_trash"
    print(f"[OK] 删除+CRC32校验: {basename} 整组已移入 {tdir}")

    # 恢复
    restored: dict = {}
    worker.group_restored.connect(lambda rid, c: restored.update(rid=rid, c=c))
    worker.restore_conflict.connect(lambda n: worker.answer_conflict("skip"))
    worker._do_restore(rec)
    assert restored.get("rid") == rec["id"]
    assert (src / f"{basename}.JPG").exists(), "恢复后源位置应有文件"
    history = trashmod.load_history(trash_root)
    assert history[0]["restored"] is True
    print(f"[OK] 恢复: {basename} 已回到原目录，manifest 已标记 restored")


def test_crc32_detects_corruption(tmp: Path) -> None:
    from photo_culler.core.fileops import crc32_of
    a = tmp / "a.bin"
    b = tmp / "b.bin"
    a.write_bytes(b"\x01" * 10000)
    b.write_bytes(b"\x01" * 9999 + b"\x02")
    assert crc32_of(a) != crc32_of(b), "不同内容 CRC32 应不同"
    b.write_bytes(b"\x01" * 10000)
    assert crc32_of(a) == crc32_of(b), "相同内容 CRC32 应相同"
    print("[OK] CRC32 校验逻辑正确")


def test_flat_layout_and_conflict(tmp: Path) -> None:
    """平铺布局：文件直接进回收根目录；不同来源同 basename 时自动加序号。"""
    from photo_culler.core.fileops import FileOpWorker

    src1 = tmp / "sd1"
    src2 = tmp / "sd2"
    make_fake_photos(src1, ["IMG_0001"])
    make_fake_photos(src2, ["IMG_0001"])
    trash_root = tmp / "trash"

    worker = FileOpWorker()
    recs: list[dict] = []
    worker.group_deleted.connect(lambda b, r: recs.append(r))

    g1 = scan_directory(src1, [".jpg"], [".cr3"])[0]
    g2 = scan_directory(src2, [".jpg"], [".cr3"])[0]
    worker._do_delete(g1, trash_root)
    worker._do_delete(g2, trash_root)
    assert len(recs) == 2, f"两次删除都应成功: {recs}"

    # 平铺：trash_dir 就是回收根目录
    assert Path(recs[0]["trash_dir"]) == trash_root
    assert Path(recs[1]["trash_dir"]) == trash_root
    assert recs[0].get("trash_root") == str(trash_root)
    # 第一组原文件名，第二组重名加序号
    names1 = {f["name"] for f in recs[0]["files"]}
    names2 = {f["name"] for f in recs[1]["files"]}
    assert names1 == {"IMG_0001.JPG", "IMG_0001.CR3"}, names1
    assert names2 == {"IMG_0001_1.JPG", "IMG_0001_1.CR3"}, names2
    for n in names1 | names2:
        assert (trash_root / n).exists(), n
    print(f"[OK] 平铺布局+冲突重命名: {sorted(names1)} / {sorted(names2)}")

    # 恢复第二组（带序号的记录）应还原为原始文件名
    worker.restore_conflict.connect(lambda n: worker.answer_conflict("skip"))
    worker._do_restore(recs[1])
    assert (src2 / "IMG_0001.JPG").exists(), "恢复后应还原为原文件名"
    print("[OK] 平铺恢复: 重命名副本已还原为原始文件名")


def test_metadata_readers(tmp: Path) -> None:
    """JPG：EXIF 拍摄时间 + XMP 评分；CR3：moov/uuid 中的 XMP 评分。"""
    jpg = tmp / "meta" / "IMG_A.JPG"
    make_real_jpeg(jpg, rating=4, dt="2026:09:05 17:50:12")
    info = metamod.read_media_info(jpg)
    assert info.rating == 4, f"JPG 评分应为 4，实际 {info.rating}"
    day = datetime.fromtimestamp(info.capture_ts).strftime("%Y-%m-%d %H:%M")
    assert day == "2026-09-05 17:50", f"JPG 拍摄时间不对: {day}"

    jpg0 = tmp / "meta" / "IMG_B.JPG"
    make_real_jpeg(jpg0, rating=None, dt="2026:09:06 08:00:00")
    assert metamod.read_media_info(jpg0).rating == 0, "无 XMP 应视为未评分"

    cr3 = tmp / "meta" / "IMG_A.CR3"
    make_fake_cr3(cr3, 3)
    assert metamod.read_media_info(cr3).rating == 3, "CR3 评分解析失败"
    print("[OK] 元数据读取: JPG 评分/拍摄时间、CR3 评分 均正确")


def test_scan_reads_metadata(tmp: Path) -> None:
    src = tmp / "sd_meta"
    make_real_jpeg(src / "IMG_1.JPG", rating=5, dt="2026:09:01 08:00:00")
    make_fake_cr3(src / "IMG_1.CR3", 5)
    make_real_jpeg(src / "IMG_2.JPG", rating=None, dt="2026:09:02 09:30:00")

    groups = scan_directory(src, [".jpg", ".jpeg"], [".cr3"])
    g1 = next(g for g in groups if g.basename == "IMG_1")
    g2 = next(g for g in groups if g.basename == "IMG_2")
    assert g1.rating == 5 and g1.day == "2026-09-01", (g1.rating, g1.day)
    assert g2.rating == 0 and g2.day == "2026-09-02", (g2.rating, g2.day)
    print("[OK] 扫描接入元数据: 组上带出评分与拍摄日")


def test_filters(tmp: Path) -> None:
    src = tmp / "sd_filter"
    make_real_jpeg(src / "IMG_1.JPG", rating=5, dt="2026:09:01 08:00:00")
    make_real_jpeg(src / "IMG_2.JPG", rating=3, dt="2026:09:01 09:00:00")
    make_real_jpeg(src / "IMG_3.JPG", rating=0, dt="2026:09:02 10:00:00")
    groups = scan_directory(src, [".jpg"], [".cr3"])

    assert len(filters.visible_indexes(groups, days={"2026-09-01"})) == 2
    assert len(filters.visible_indexes(groups, days={"2026-09-02"})) == 1
    # 日期多选：两个拍摄日都看
    assert len(filters.visible_indexes(groups, days={"2026-09-01", "2026-09-02"})) == 3
    # 评分多选：只看 3 星或 5 星
    assert len(filters.visible_indexes(groups, ratings={3, 5})) == 2
    assert len(filters.visible_indexes(groups, ratings={4, 5})) == 1     # 4 星以上
    assert len(filters.visible_indexes(groups, ratings={0})) == 1        # 未评分
    assert len(filters.visible_indexes(groups, days={"2026-09-01"},
                                       ratings={3, 5})) == 2
    assert len(filters.visible_indexes(groups, days={"2026-09-02"},
                                       ratings={3, 5})) == 0
    # 空集合 = 不筛选
    assert len(filters.visible_indexes(groups)) == 3
    assert len(filters.visible_indexes(groups, days=set(), ratings=set())) == 3
    print("[OK] 筛选条件: 日期多选 / 评分多选 / 未评分 / 组合 均正确")


def test_mark_siblings_safe(tmp: Path) -> None:
    """取消标记只删精确副本（原名或 _n），不碰导出目录里的无关文件。"""
    from photo_culler.core.fileops import FileOpWorker
    export = tmp / "export"
    export.mkdir(parents=True)
    for n in ("IMG_0001.JPG", "IMG_0001_1.JPG", "IMG_0001x.JPG",
              "IMG_00011.JPG", "IMG_0001.CR3"):
        (export / n).write_bytes(b"x")

    worker = FileOpWorker()
    pf_jpg = PhotoFile(path=tmp / "IMG_0001.JPG", ext=".jpg", size=1, mtime=0,
                       role="preview")
    hits = {p.name for p in worker._mark_siblings(export, pf_jpg)}
    assert hits == {"IMG_0001.JPG", "IMG_0001_1.JPG"}, hits
    pf_cr3 = PhotoFile(path=tmp / "IMG_0001.CR3", ext=".cr3", size=1, mtime=0,
                       role="sidecar")
    hits_cr3 = {p.name for p in worker._mark_siblings(export, pf_cr3)}
    assert hits_cr3 == {"IMG_0001.CR3"}, hits_cr3
    print("[OK] 取消标记安全匹配: 仅命中原名/序号副本")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        test_scan_and_group(tmp)
        test_delete_and_crc32(tmp)
        test_crc32_detects_corruption(tmp)
        test_flat_layout_and_conflict(tmp)
        test_metadata_readers(tmp)
        test_scan_reads_metadata(tmp)
        test_filters(tmp)
        test_mark_siblings_safe(tmp)
    print("\n全部测试通过 ✓")
