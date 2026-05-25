"""压缩包 (.zip / .rar / .7z / .tar / .tar.gz / .tgz / .gz) 转换器。

核心思路：压缩包的内容是压缩后的二进制流，对 LLM 无意义。
但**文件列表**（路径 + 大小 + CRC + 压缩比）信息密度极高，
能让 LLM 快速理解「这个压缩包是什么、装了哪些文件」。

支持矩阵：
- .zip       : zipfile（Python 标准库）✓
- .tar.gz/.tgz/.tar : tarfile（标准库）✓
- .7z        : py7zr（可选依赖）；缺少时回落到「仅元信息 + hex」
- .rar       : rarfile（可选依赖，需要 unrar 工具）；缺少时回落到「仅元信息 + hex」
- .gz (单文件): gzip（标准库）✓ —— 只能拿到原始文件名 + mtime

所有路径都会在「列表完成后」再附「头部 + 尾部 hex 预览」（默认各 256 字节），
便于 LLM 用魔数交叉验证。
"""
from __future__ import annotations

import gzip
import hashlib
import importlib.util
import logging
import struct
import tarfile
import zipfile
from pathlib import Path

from .base import BaseConverter, ConversionResult

logger = logging.getLogger(__name__)


_HEAD_HEX_BYTES = 256
_TAIL_HEX_BYTES = 256
_MAX_LIST_ENTRIES = 5000  # 极端大压缩包的安全上限


def _hex_preview(data: bytes, *, bytes_per_line: int = 16) -> str:
    if not data:
        return "(空)\n"
    lines = []
    for offset in range(0, len(data), bytes_per_line):
        chunk = data[offset : offset + bytes_per_line]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{offset:08x}  {hex_part:<{bytes_per_line * 3 - 1}}  |{ascii_part}|")
    return "\n".join(lines) + "\n"


def _read_head_tail(path: Path, size: int) -> tuple[bytes, bytes]:
    try:
        with path.open("rb") as f:
            head = f.read(_HEAD_HEX_BYTES)
            if size > _HEAD_HEX_BYTES + _TAIL_HEX_BYTES:
                f.seek(max(0, size - _TAIL_HEX_BYTES))
                tail = f.read(_TAIL_HEX_BYTES)
            else:
                tail = b""
    except OSError:
        head, tail = b"", b""
    return head, tail


def _sha1_streaming(path: Path) -> str:
    try:
        sha1 = hashlib.sha1()
        with path.open("rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                sha1.update(chunk)
        return sha1.hexdigest()
    except Exception:
        return "(计算失败)"


def _format_archive_md(
    *,
    archive_type: str,
    path: Path,
    size: int,
    sha1: str,
    entries: list[dict],
    capped: bool,
    head: bytes,
    tail: bytes,
    extra_lines: list[str] | None = None,
    decode_warnings: list[str] | None = None,
) -> str:
    """统一渲染压缩包的 markdown。"""
    parts: list[str] = []
    parts.append(f"**{archive_type} 压缩包（仅文件列表 + 头尾 hex）**\n\n")
    parts.append(f"- 文件大小: {size:,} bytes\n")
    parts.append(f"- SHA-1: `{sha1}`\n")
    parts.append(f"- 内含条目数: {len(entries)}"
                 + (f" *（达到上限 {_MAX_LIST_ENTRIES:,}，未列完）*" if capped else "")
                 + "\n")
    total_uncompressed = sum(e.get("size", 0) for e in entries if isinstance(e.get("size"), int))
    if total_uncompressed:
        ratio = (size / total_uncompressed * 100) if total_uncompressed else 0
        parts.append(f"- 解压后总大小: {total_uncompressed:,} bytes（压缩比 ≈ {ratio:.1f}%）\n")
    if extra_lines:
        for line in extra_lines:
            parts.append(f"- {line}\n")
    parts.append("\n")

    if entries:
        parts.append("### 内含文件列表\n\n")
        # 是否有 CRC 列
        has_crc = any("crc" in e for e in entries)
        if has_crc:
            parts.append("| # | 路径 | size_bytes | compressed | CRC32 | mtime |\n")
            parts.append("|---|------|-----------:|-----------:|-------|-------|\n")
        else:
            parts.append("| # | 路径 | size_bytes | mtime |\n")
            parts.append("|---|------|-----------:|-------|\n")
        for i, e in enumerate(entries, start=1):
            name = str(e.get("name", "?")).replace("|", "\\|")
            sz = e.get("size", "?")
            sz_str = f"{sz:,}" if isinstance(sz, int) else str(sz)
            mt = e.get("mtime", "")
            if has_crc:
                cs = e.get("csize", "?")
                cs_str = f"{cs:,}" if isinstance(cs, int) else str(cs)
                crc = e.get("crc", "")
                crc_str = f"{crc:08x}" if isinstance(crc, int) else str(crc)
                parts.append(f"| {i} | `{name}` | {sz_str} | {cs_str} | {crc_str} | {mt} |\n")
            else:
                parts.append(f"| {i} | `{name}` | {sz_str} | {mt} |\n")
        parts.append("\n")
    else:
        parts.append("_（未能列出条目，可能是未安装可选依赖或文件损坏）_\n\n")

    if decode_warnings:
        parts.append("> ⚠️ 解析过程中的警告：\n")
        for w in decode_warnings:
            parts.append(f"> - {w}\n")
        parts.append("\n")

    parts.append(f"### 头部 hex（前 {len(head)} bytes，可用于魔数交叉验证）\n\n```\n")
    parts.append(_hex_preview(head))
    parts.append("```\n\n")

    if tail:
        parts.append(f"### 尾部 hex（最后 {len(tail)} bytes）\n\n```\n")
        parts.append(_hex_preview(tail))
        parts.append("```\n")

    return "".join(parts)


# ---------------- 各类型实现 ----------------

def _list_zip(path: Path) -> tuple[list[dict], bool, list[str]]:
    warnings: list[str] = []
    capped = False
    entries: list[dict] = []
    try:
        with zipfile.ZipFile(path, "r") as zf:
            for i, info in enumerate(zf.infolist()):
                if i >= _MAX_LIST_ENTRIES:
                    capped = True
                    break
                # date_time 是 6 元组
                try:
                    import datetime as _dt
                    mt = _dt.datetime(*info.date_time).isoformat(timespec="seconds")
                except Exception:
                    mt = ""
                entries.append({
                    "name": info.filename,
                    "size": info.file_size,
                    "csize": info.compress_size,
                    "crc": info.CRC,
                    "mtime": mt,
                })
    except zipfile.BadZipFile as e:
        warnings.append(f"非法的 zip 结构：{e}")
    except Exception as e:
        warnings.append(f"zip 解析异常：{e.__class__.__name__}: {e}")
    return entries, capped, warnings


def _list_tar(path: Path) -> tuple[list[dict], bool, list[str]]:
    warnings: list[str] = []
    capped = False
    entries: list[dict] = []
    try:
        with tarfile.open(path, "r:*") as tf:
            for i, member in enumerate(tf):
                if i >= _MAX_LIST_ENTRIES:
                    capped = True
                    break
                import datetime as _dt
                try:
                    mt = _dt.datetime.fromtimestamp(member.mtime).isoformat(timespec="seconds")
                except Exception:
                    mt = ""
                entries.append({
                    "name": member.name,
                    "size": member.size,
                    "mtime": mt,
                })
    except tarfile.TarError as e:
        warnings.append(f"非法的 tar 结构：{e}")
    except Exception as e:
        warnings.append(f"tar 解析异常：{e.__class__.__name__}: {e}")
    return entries, capped, warnings


def _list_7z(path: Path) -> tuple[list[dict], bool, list[str]]:
    warnings: list[str] = []
    if importlib.util.find_spec("py7zr") is None:
        warnings.append(
            "未安装 py7zr，无法列出 .7z 内含文件；如需列表请执行 `pip install py7zr`"
        )
        return [], False, warnings
    try:
        import py7zr  # type: ignore
    except Exception as e:
        warnings.append(f"py7zr 导入失败：{e}")
        return [], False, warnings

    capped = False
    entries: list[dict] = []
    try:
        with py7zr.SevenZipFile(path, mode="r") as zf:
            info_list = zf.list()
            for i, info in enumerate(info_list):
                if i >= _MAX_LIST_ENTRIES:
                    capped = True
                    break
                mt = ""
                if getattr(info, "creationtime", None):
                    try:
                        mt = info.creationtime.isoformat(timespec="seconds")
                    except Exception:
                        pass
                entries.append({
                    "name": info.filename,
                    "size": info.uncompressed,
                    "csize": getattr(info, "compressed", "?"),
                    "crc": info.crc32 if info.crc32 is not None else "",
                    "mtime": mt,
                })
    except Exception as e:
        warnings.append(f"7z 解析异常：{e.__class__.__name__}: {e}")
    return entries, capped, warnings


def _list_rar(path: Path) -> tuple[list[dict], bool, list[str]]:
    warnings: list[str] = []
    if importlib.util.find_spec("rarfile") is None:
        warnings.append(
            "未安装 rarfile（且需要 unrar 工具），无法列出 .rar 内含文件；"
            "如需列表请执行 `pip install rarfile` 并安装 unrar"
        )
        return [], False, warnings
    try:
        import rarfile  # type: ignore
    except Exception as e:
        warnings.append(f"rarfile 导入失败：{e}")
        return [], False, warnings

    capped = False
    entries: list[dict] = []
    try:
        with rarfile.RarFile(path) as rf:
            for i, info in enumerate(rf.infolist()):
                if i >= _MAX_LIST_ENTRIES:
                    capped = True
                    break
                import datetime as _dt
                try:
                    mt = _dt.datetime(*info.date_time).isoformat(timespec="seconds")
                except Exception:
                    mt = ""
                entries.append({
                    "name": info.filename,
                    "size": info.file_size,
                    "csize": info.compress_size,
                    "crc": getattr(info, "CRC", ""),
                    "mtime": mt,
                })
    except Exception as e:
        warnings.append(f"rar 解析异常：{e.__class__.__name__}: {e}")
    return entries, capped, warnings


def _list_gz_single(path: Path) -> tuple[list[dict], list[str]]:
    """单文件 .gz（不是 tar.gz）：从 gzip 头中尝试取原始文件名 + mtime。"""
    warnings: list[str] = []
    entries: list[dict] = []
    try:
        with path.open("rb") as f:
            head = f.read(512)
        if len(head) < 10 or head[:2] != b"\x1f\x8b":
            warnings.append("不是合法的 gzip 头")
            return entries, warnings
        flags = head[3]
        mtime = struct.unpack_from("<I", head, 4)[0]
        # 解析 FNAME（如有）：以 \x00 结尾的字符串
        offset = 10
        # 跳过 FEXTRA
        if flags & 0x04 and offset + 2 <= len(head):
            xlen = struct.unpack_from("<H", head, offset)[0]
            offset += 2 + xlen
        name = ""
        if flags & 0x08:  # FNAME
            end = head.find(b"\x00", offset)
            if end != -1:
                name = head[offset:end].decode("latin-1", errors="replace")
        import datetime as _dt
        try:
            mt_iso = _dt.datetime.utcfromtimestamp(mtime).isoformat() + "Z" if mtime else ""
        except Exception:
            mt_iso = ""
        entries.append({
            "name": name or "(未在 gzip 头中记录)",
            "size": "?",  # gzip 单文件不在头里存 uncompressed size
            "mtime": mt_iso,
        })
    except Exception as e:
        warnings.append(f"gzip 头解析异常：{e}")
    return entries, warnings


# ---------------- 主 converter ----------------

class ArchiveConverter(BaseConverter):
    """压缩包通用 converter；根据 kind 调用不同实现。"""

    # kind 占位；实际通过 make_archive_converter 设置
    kind = "archive_zip"

    def convert(self, path: Path) -> ConversionResult:
        try:
            size = path.stat().st_size
        except OSError as e:
            return ConversionResult(
                status="failed", markdown="",
                error=f"读取失败: {e.__class__.__name__}: {e}",
            )

        sha1 = _sha1_streaming(path)
        head, tail = _read_head_tail(path, size)
        entries: list[dict] = []
        capped = False
        warnings: list[str] = []
        extra_lines: list[str] = []

        suffix = path.suffix.lower()
        # tar 系列单独判定（.tar / .tar.gz / .tgz / .tar.bz2 等）
        name_lower = path.name.lower()
        is_tar = (
            self.kind == "archive_tar"
            or name_lower.endswith((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz"))
        )

        if self.kind == "archive_zip":
            archive_type = "ZIP"
            entries, capped, warnings = _list_zip(path)
        elif is_tar:
            archive_type = "TAR"
            entries, capped, warnings = _list_tar(path)
        elif self.kind == "archive_7z":
            archive_type = "7-Zip"
            entries, capped, warnings = _list_7z(path)
        elif self.kind == "archive_rar":
            archive_type = "RAR"
            entries, capped, warnings = _list_rar(path)
        elif self.kind == "archive_gz":
            archive_type = "Gzip (单文件)"
            entries, warnings = _list_gz_single(path)
        else:
            archive_type = f"压缩包 ({self.kind})"

        md = _format_archive_md(
            archive_type=archive_type,
            path=path,
            size=size,
            sha1=sha1,
            entries=entries,
            capped=capped,
            head=head,
            tail=tail,
            extra_lines=extra_lines,
            decode_warnings=warnings,
        )

        return ConversionResult(
            status="success",
            markdown=md,
            warnings=warnings,
            extra={
                "archive_type": archive_type,
                "original_bytes": size,
                "sha1": sha1,
                "entries_count": len(entries),
                "entries_capped": capped,
            },
        )


def make_archive_converter(kind: str):
    """根据 kind 动态生成 ArchiveConverter 的子类（让 kind 属性正确）。"""

    class _ArchiveConverter(ArchiveConverter):
        pass

    _ArchiveConverter.kind = kind
    _ArchiveConverter.__name__ = f"ArchiveConverter_{kind}"
    return _ArchiveConverter


ALL_ARCHIVE_KINDS = ("archive_zip", "archive_tar", "archive_7z", "archive_rar", "archive_gz")
