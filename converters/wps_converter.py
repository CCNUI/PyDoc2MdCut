"""金山 WPS 文档 (.wps) 转换器。

WPS Office 的 .wps 历史上有多种载体：
  1. 现代 WPS Office 偶尔把 OOXML（实质是 zip）保存成 .wps；
  2. 旧版 WPS / Microsoft Works 用 OLE 复合文档（魔数 D0 CF 11 E0）；
  3. 极少数老版本是私有 CCF 二进制格式。

本转换器按以下顺序尝试：
  a) 嗅探魔数；
  b) 是 zip → 当 OOXML 跑 mammoth；
  c) 是 OLE → 调 LibreOffice headless 转 docx 后再 mammoth（若环境有 soffice）；
     若没有 LibreOffice，尝试用 olefile 抽取可见文本流（粗糙但聊胜于无）；
  d) 都失败 → 失败占位，并在报告中给出建议（用 WPS Office 另存为 .docx）。

LibreOffice 检测：环境变量 LIBREOFFICE_BIN（优先）或 PATH 中的 `soffice` / `libreoffice`。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .base import BaseConverter, ConversionResult

logger = logging.getLogger(__name__)


_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ZIP_MAGIC = b"PK\x03\x04"


def _detect_format(raw_head: bytes) -> str:
    """根据魔数判定。返回 'ooxml' / 'ole' / 'unknown'。"""
    if raw_head.startswith(_ZIP_MAGIC):
        return "ooxml"
    if raw_head.startswith(_OLE_MAGIC):
        return "ole"
    return "unknown"


def _find_libreoffice() -> str | None:
    """寻找 LibreOffice 可执行文件路径。"""
    env_bin = os.environ.get("LIBREOFFICE_BIN", "").strip()
    if env_bin and Path(env_bin).exists():
        return env_bin
    for name in ("soffice", "libreoffice"):
        which = shutil.which(name)
        if which:
            return which
    # Windows 默认安装路径
    if os.name == "nt":
        for guess in (
            r"C:\Program Files\LibreOffice\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        ):
            if Path(guess).exists():
                return guess
    return None


class WpsConverter(BaseConverter):
    """金山 WPS 文档。"""

    kind = "wps"

    def convert(self, path: Path) -> ConversionResult:
        try:
            with path.open("rb") as f:
                head = f.read(16)
        except OSError as e:
            return ConversionResult(
                status="failed",
                markdown="",
                error=f"读取失败: {e.__class__.__name__}: {e}",
            )

        fmt = _detect_format(head)
        warnings: list[str] = [f"WPS 格式探测: {fmt}"]

        # ---- a) OOXML 路径 ----
        if fmt == "ooxml":
            md, sub_warns, err = self._try_mammoth(path)
            warnings.extend(sub_warns)
            if md is not None:
                return ConversionResult(
                    status="success",
                    markdown=self._wrap(md, format_label="OOXML（zip）"),
                    warnings=warnings,
                    extra={"chars": len(md), "engine": "mammoth", "wps_format": "ooxml"},
                )
            warnings.append(f"mammoth 失败：{err}；尝试 LibreOffice")

        # ---- c1) LibreOffice 路径（OLE 或 OOXML 兜底）----
        soffice = _find_libreoffice()
        if soffice:
            md, sub_warns, err = self._try_libreoffice(path, soffice)
            warnings.extend(sub_warns)
            if md is not None:
                return ConversionResult(
                    status="success",
                    markdown=self._wrap(md, format_label=f"经 LibreOffice 转 docx → markdown"),
                    warnings=warnings,
                    extra={
                        "chars": len(md),
                        "engine": "libreoffice+mammoth",
                        "wps_format": fmt,
                    },
                )
            warnings.append(f"LibreOffice 路径失败：{err}")
        else:
            warnings.append(
                "未发现 LibreOffice（soffice）。"
                "Windows 安装包: https://www.libreoffice.org/download/ ；"
                "UOS/Debian: sudo apt install libreoffice"
            )

        # ---- c2) olefile 文本抽取（仅对 OLE 格式）----
        if fmt == "ole":
            md, sub_warns, err = self._try_olefile(path)
            warnings.extend(sub_warns)
            if md is not None:
                return ConversionResult(
                    status="success",
                    markdown=self._wrap(md, format_label="OLE compound（粗略文本抽取）"),
                    warnings=warnings,
                    extra={
                        "chars": len(md),
                        "engine": "olefile",
                        "wps_format": "ole",
                    },
                )
            warnings.append(f"olefile 路径失败：{err}")

        # ---- d) 全部失败 ----
        size = path.stat().st_size if path.exists() else 0
        placeholder = (
            "> ❌ **WPS 文件转换失败**\n"
            f"> - 文件: {path.name}\n"
            f"> - 大小: {size:,} bytes\n"
            f"> - 探测格式: {fmt}\n"
            "> - 已尝试: mammoth / LibreOffice / olefile，全部失败\n"
            ">\n"
            "> 💡 **建议**: 在 WPS Office 中打开此文件，"
            "另存为 .docx 后重跑本工具，可获得完整内容。\n"
        )
        return ConversionResult(
            status="failed",
            markdown=placeholder,
            warnings=warnings,
            error=f"WPS 转换失败：所有路径均不可用（探测格式={fmt}）",
            extra={"wps_format": fmt},
        )

    # ---------- 各路径实现 ----------
    @staticmethod
    def _try_mammoth(path: Path) -> tuple[str | None, list[str], str | None]:
        warnings: list[str] = []
        try:
            import mammoth  # type: ignore
            with path.open("rb") as f:
                result = mammoth.convert_to_markdown(f)
            md = result.value or ""
            for msg in result.messages:
                lvl = getattr(msg, "type", "info")
                if lvl in ("warning", "error"):
                    warnings.append(f"mammoth-{lvl}: {msg.message}")
            if md.strip():
                if not md.endswith("\n"):
                    md += "\n"
                return md, warnings, None
            return None, warnings, "mammoth 输出为空"
        except Exception as e:
            return None, warnings, f"{e.__class__.__name__}: {e}"

    @staticmethod
    def _try_libreoffice(
        path: Path, soffice_bin: str
    ) -> tuple[str | None, list[str], str | None]:
        """用 LibreOffice headless 把 .wps 转成 .docx，再走 mammoth。"""
        warnings: list[str] = []
        try:
            import mammoth  # type: ignore
        except Exception as e:
            return None, warnings, f"mammoth 不可用: {e}"

        with tempfile.TemporaryDirectory(prefix="wps_conv_") as td:
            tmpdir = Path(td)
            try:
                proc = subprocess.run(
                    [
                        soffice_bin,
                        "--headless",
                        "--convert-to", "docx",
                        "--outdir", str(tmpdir),
                        str(path),
                    ],
                    timeout=120,
                    capture_output=True,
                    text=True,
                )
            except subprocess.TimeoutExpired:
                return None, warnings, "LibreOffice 转换超时（120s）"
            except Exception as e:
                return None, warnings, f"LibreOffice 启动失败: {e.__class__.__name__}: {e}"

            if proc.returncode != 0:
                err_tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
                return None, warnings, f"LibreOffice 返回非零: {' / '.join(err_tail)}"

            # 找 docx
            docx_path = tmpdir / (path.stem + ".docx")
            if not docx_path.exists():
                # 容忍 LibreOffice 的命名差异
                cands = list(tmpdir.glob("*.docx"))
                if not cands:
                    return None, warnings, "LibreOffice 未生成 .docx 文件"
                docx_path = cands[0]

            try:
                with docx_path.open("rb") as f:
                    result = mammoth.convert_to_markdown(f)
                md = result.value or ""
                for msg in result.messages:
                    lvl = getattr(msg, "type", "info")
                    if lvl in ("warning", "error"):
                        warnings.append(f"mammoth-{lvl}: {msg.message}")
                if not md.strip():
                    return None, warnings, "mammoth 在转出的 docx 上仍输出为空"
                if not md.endswith("\n"):
                    md += "\n"
                return md, warnings, None
            except Exception as e:
                return None, warnings, f"mammoth 处理 docx 失败: {e.__class__.__name__}: {e}"

    @staticmethod
    def _try_olefile(path: Path) -> tuple[str | None, list[str], str | None]:
        """对 OLE 复合文档做粗糙的文本抽取（最后兜底）。"""
        warnings: list[str] = []
        try:
            import olefile  # type: ignore
        except Exception:
            return None, warnings, "olefile 未安装（pip install olefile）"

        try:
            ole = olefile.OleFileIO(str(path))
        except Exception as e:
            return None, warnings, f"olefile 打开失败: {e.__class__.__name__}: {e}"

        snippets: list[str] = []
        try:
            for stream in ole.listdir(streams=True):
                name = "/".join(stream)
                # 尝试从可能的文本流抽取（WordDocument / 1Table / WpsDocument 等）
                if any(k in name.lower() for k in ("word", "wps", "text", "1table", "0table")):
                    try:
                        data = ole.openstream(stream).read()
                        snippets.append(_extract_printable_runs(data, source=name))
                    except Exception as e:
                        warnings.append(f"读 {name} 失败: {e}")
        finally:
            ole.close()

        text = "\n\n".join(s for s in snippets if s.strip())
        if not text.strip():
            return None, warnings, "未能从 OLE 流中抽取到任何可读文本"

        warnings.append("olefile 路径只做粗略文本抽取，格式信息（标题/表格/列表）会丢失")
        # 包成 fenced 块，避免乱七八糟的字符破坏 MD
        md = "```text\n" + text + "\n```\n"
        return md, warnings, None

    # ---------- 工具 ----------
    @staticmethod
    def _wrap(md_body: str, *, format_label: str) -> str:
        """给正文加一个简短的 WPS 元数据头。"""
        head = f"**金山 WPS 文档**（处理路径: {format_label}）\n\n"
        return head + md_body


def _extract_printable_runs(data: bytes, *, source: str) -> str:
    """从二进制流里抽出连续的可打印 ASCII 段；同时尝试 utf-16-le（Word 常用）。"""
    parts: list[str] = []

    # 1) ASCII 段
    runs = re.findall(rb"[\x09\x0a\x0d\x20-\x7e]{4,}", data)
    if runs:
        ascii_text = b"\n".join(runs).decode("ascii", errors="replace")
        parts.append(f"# 来源流: {source} (ASCII)\n{ascii_text}")

    # 2) UTF-16-LE 段（Word 系常用）
    try:
        u = data.decode("utf-16-le", errors="replace")
        # 抽连续可打印
        matches = re.findall(r"[\u0020-\u007e\u4e00-\u9fff\u3000-\u303f\uff00-\uffef\s]{4,}", u)
        if matches:
            joined = "\n".join(m.strip() for m in matches if m.strip())
            if joined:
                parts.append(f"# 来源流: {source} (UTF-16-LE)\n{joined}")
    except Exception:
        pass

    return "\n\n".join(parts)
