"""MissionPlanner / ArduPilot 参数备份文件 (.param) 转换器。

.param 文件是简单的文本格式，每行一个参数：
    PARAM_NAME,VALUE      # 逗号分隔
    PARAM_NAME VALUE      # 空格/Tab 分隔
    # 行首 # 是注释
约定：
    - UTF-8 优先；探测失败回落 charset-normalizer
    - 解析为参数 → 输出 markdown 表格（参数名 | 值）
    - 同时附原文代码块，避免遗漏注释 / 节段头
    - 解析 0 行也算成功（空文件）
"""
from __future__ import annotations

import logging
from pathlib import Path

from charset_normalizer import from_bytes

from .base import BaseConverter, ConversionResult

logger = logging.getLogger(__name__)


def _decode_with_fallback(raw: bytes) -> tuple[str, str]:
    """UTF-8 优先；失败用 charset-normalizer。返回 (text, encoding)。"""
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        best = from_bytes(raw).best()
        if best is not None:
            return str(best), (best.encoding or "unknown")
        return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def _parse_lines(text: str) -> list[tuple[str, str]]:
    """解析 .param 文本为 [(name, value), ...]。注释/空行跳过。"""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        # 兼容逗号 / 空白
        if "," in s:
            parts = s.split(",", 1)
        else:
            parts = s.split(None, 1)
        if len(parts) != 2:
            continue
        name, value = parts[0].strip(), parts[1].strip()
        if not name:
            continue
        out.append((name, value))
    return out


def _md_table(rows: list[tuple[str, str]]) -> str:
    """生成 markdown 表格。已对 | 做转义。"""
    if not rows:
        return "_（未解析到参数行）_\n"
    lines = ["| 参数 | 值 |", "| --- | --- |"]
    for name, value in rows:
        n = name.replace("|", "\\|")
        v = value.replace("|", "\\|")
        lines.append(f"| {n} | {v} |")
    return "\n".join(lines) + "\n"


class ParamConverter(BaseConverter):
    """MissionPlanner / ArduPilot 参数备份。"""

    kind = "param"

    def convert(self, path: Path) -> ConversionResult:
        try:
            raw = path.read_bytes()
        except OSError as e:
            return ConversionResult(
                status="failed",
                markdown="",
                error=f"读取失败: {e.__class__.__name__}: {e}",
            )

        # pre-read 截取
        raw, _info, pre_warnings = self._pre_read_truncate_bytes(raw, path=path)

        text, encoding = _decode_with_fallback(raw)
        warnings: list[str] = list(pre_warnings)
        if encoding not in ("utf-8", "utf_8", "ascii"):
            warnings.append(f"原文件编码非 UTF-8（探测为 {encoding}），已转码")

        rows = _parse_lines(text)
        n_params = len(rows)
        n_lines = len(text.splitlines())
        n_comments = sum(1 for ln in text.splitlines() if ln.lstrip().startswith("#"))

        # 头部摘要 + 表格 + 原文（fenced 代码块）
        parts: list[str] = []
        parts.append("**MissionPlanner / ArduPilot 参数备份**\n\n")
        parts.append(f"- 总行数: {n_lines}\n")
        parts.append(f"- 解析到参数: {n_params}\n")
        parts.append(f"- 注释行: {n_comments}\n")
        parts.append(f"- 文件编码: {encoding}\n\n")
        parts.append("### 参数表\n\n")
        parts.append(_md_table(rows))
        parts.append("\n### 原文\n\n")
        parts.append("```text\n")
        parts.append(text)
        if not text.endswith("\n"):
            parts.append("\n")
        parts.append("```\n")

        md = "".join(parts)
        return ConversionResult(
            status="success",
            markdown=md,
            warnings=warnings,
            extra={
                "chars": len(text),
                "encoding": encoding,
                "param_count": n_params,
                "comment_lines": n_comments,
            },
        )
