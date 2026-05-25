"""旧版 Office 文件 (.doc / .xls / .ppt 等 OLE 复合二进制) 的通用 olefile 文本抽取。

设计目的:
    用户不愿意装 LibreOffice → 没有渲染路径,只能做粗糙的"打开二进制,扫可读字串"。
    这是 olefile 唯一能做的事——它读出 OLE 流,但流内是私有二进制,
    格式信息(表头/段落/单元格)无法重建。但**对 LLM 阅读相关文档摘要**已经够用。

输出特点:
    - 文档头有 OLE 探测信息和"已知局限"提示,LLM 自己会判断
    - 文本块包装在 fenced ```text 里,免得乱码破坏 markdown
    - 完全失败 (非 OLE / 无文本流) → status=failed,占位文本含 LibreOffice 提示
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from .base import ConversionResult

logger = logging.getLogger(__name__)


_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _detect_ole(path: Path) -> tuple[bool, bytes]:
    """探测前 8 字节是否为 OLE 复合文档魔数。"""
    try:
        with path.open("rb") as f:
            head = f.read(8)
        return head.startswith(_OLE_MAGIC), head
    except OSError:
        return False, b""


def _extract_text_from_ole(
    path: Path,
    *,
    stream_filter_keywords: tuple[str, ...] = (
        "word", "wordtext", "text", "1table", "0table",
        "workbook", "book", "wpsdocument", "pptdocument",
        "powerpoint", "currentuser", "pictures",
    ),
    max_streams: int = 100,
) -> tuple[str, list[str]]:
    """打开 OLE 文件,从可疑流里抽出可打印文本。

    Returns:
        (text, warnings)。text 为空字符串表示没抽到任何东西。
    """
    warnings: list[str] = []
    try:
        import olefile  # type: ignore
    except ImportError:
        warnings.append("olefile 未安装 (pip install olefile),无法抽取")
        return "", warnings

    try:
        ole = olefile.OleFileIO(str(path))
    except Exception as e:
        warnings.append(f"olefile 打开失败: {e.__class__.__name__}: {e}")
        return "", warnings

    snippets: list[str] = []
    streams_seen = 0
    try:
        for stream in ole.listdir(streams=True):
            if streams_seen >= max_streams:
                warnings.append(f"olefile 流数量超过 {max_streams},仅处理前 {max_streams} 个")
                break
            streams_seen += 1
            name = "/".join(stream)
            name_lc = name.lower()
            # 任何关键词命中就尝试抽
            if not any(k in name_lc for k in stream_filter_keywords):
                continue
            try:
                data = ole.openstream(stream).read()
                text_chunk = _extract_printable_runs(data, source=name)
                if text_chunk:
                    snippets.append(text_chunk)
            except Exception as e:
                warnings.append(f"读 OLE 流 '{name}' 失败: {e}")
    finally:
        ole.close()

    text = "\n\n".join(s for s in snippets if s.strip())
    return text, warnings


def _extract_printable_runs(data: bytes, *, source: str) -> str:
    """从二进制流里抽出连续可打印字串,同时尝试 ASCII 和 UTF-16-LE 两种解码。

    Word/PPT 内部常用 UTF-16-LE 存正文。Excel 工作簿用 BIFF,字符串短而散。
    """
    parts: list[str] = []

    # 1) ASCII 段:最少 6 字符以滤掉短碎片
    ascii_runs = re.findall(rb"[\x09\x0a\x0d\x20-\x7e]{6,}", data)
    if ascii_runs:
        ascii_text = b"\n".join(ascii_runs).decode("ascii", errors="replace")
        # 去掉明显是文件结构关键字的噪声(部分缓解但不彻底)
        ascii_text = _trim_obvious_noise(ascii_text)
        if ascii_text.strip():
            parts.append(f"# OLE 流: {source} (ASCII 解码)\n{ascii_text}")

    # 2) UTF-16-LE 段:Word 系常用
    try:
        u = data.decode("utf-16-le", errors="replace")
        # 抽连续可打印 ASCII + CJK + 全角
        # 不允许全是替换字符 \ufffd 的段
        u16_matches = re.findall(
            r"[\u0020-\u007e\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\s]{6,}",
            u,
        )
        if u16_matches:
            cleaned = []
            for m in u16_matches:
                m = m.strip()
                if not m:
                    continue
                # 单一字符大量重复(b'\x00\x00\x00..' 的解码)被过滤
                if len(set(m)) <= 2:
                    continue
                cleaned.append(m)
            if cleaned:
                joined = "\n".join(cleaned)
                parts.append(f"# OLE 流: {source} (UTF-16-LE 解码)\n{joined}")
    except Exception:
        pass

    return "\n\n".join(parts)


def _trim_obvious_noise(text: str) -> str:
    """去掉一些明显是结构标记的 ASCII 噪声行。"""
    # 行级过滤
    out_lines = []
    for ln in text.splitlines():
        ln_s = ln.strip()
        if not ln_s:
            continue
        # 全部是同一字符的行
        if len(set(ln_s)) <= 1 and len(ln_s) > 4:
            continue
        # 看起来像 OLE 内部 GUID / 路径
        if ln_s.startswith("Root Entry") or ln_s.startswith("ObjectPool"):
            continue
        out_lines.append(ln)
    return "\n".join(out_lines)


def convert_legacy_office(
    path: Path,
    *,
    file_label: str,    # "Word 97-2003 (.doc)" 等
    new_format: str,    # ".docx" 等
) -> ConversionResult:
    """旧版 Office 文件 → markdown 的统一入口。

    流程:
        1. 探测 OLE 魔数;不是 OLE → failed
        2. olefile 抽流文本
        3. 给出清晰的失败/部分成功提示 + LibreOffice 转换建议
    """
    is_ole, head = _detect_ole(path)
    size = path.stat().st_size if path.exists() else 0

    if not is_ole:
        return ConversionResult(
            status="failed",
            markdown=(
                f"> ❌ **此{file_label}文件无法识别**\n"
                f"> - 文件: {path.name}\n"
                f"> - 大小: {size:,} bytes\n"
                f"> - 探测: 不是 OLE 复合文档 (魔数: {head.hex(' ')[:23]})\n"
                f"> - 建议: 用 WPS / Office 打开后另存为 {new_format},再重跑本工具。\n"
            ),
            error=f"非 OLE 复合文档,头部魔数: {head.hex()[:16]}",
            extra={"legacy_office_format": file_label},
        )

    text, warnings = _extract_text_from_ole(path)
    warnings.insert(0, f"olefile 路径只做粗略文本抽取,格式信息(标题/表格/列表)会丢失")

    if not text.strip():
        return ConversionResult(
            status="failed",
            markdown=(
                f"> ❌ **此{file_label}文件抽不到文本**\n"
                f"> - 文件: {path.name}\n"
                f"> - 大小: {size:,} bytes\n"
                f"> - 探测: OLE 复合文档 ✓,但未能从已知流中抽取到任何可读字符\n"
                f"> - 可能原因: 文档以加密/压缩内嵌对象保存,olefile 看不到正文\n"
                f"> - 建议: 用 WPS / Office 打开后另存为 {new_format},再重跑本工具。\n"
                f"> - 或者: 安装 LibreOffice (https://www.libreoffice.org/) 让本工具走完整渲染路径。\n"
            ),
            warnings=warnings,
            error="olefile: 未能从任何 OLE 流中抽到可读文本",
            extra={"legacy_office_format": file_label},
        )

    # 部分成功:有文本,但只是粗略抽取
    md_parts = [
        f"**{file_label}** (olefile 粗略文本抽取)\n\n",
        f"> ⚠️ **格式信息已丢失**: 标题/段落样式/表格结构/单元格位置等无法恢复。\n",
        f"> 如需保留格式,用 WPS/Office 另存为 {new_format} 或装 LibreOffice 后重跑。\n\n",
        "```text\n",
        text,
    ]
    if not text.endswith("\n"):
        md_parts.append("\n")
    md_parts.append("```\n")
    md = "".join(md_parts)

    return ConversionResult(
        status="success",
        markdown=md,
        warnings=warnings,
        extra={
            "chars": len(md),
            "engine": "olefile",
            "legacy_office_format": file_label,
        },
    )
