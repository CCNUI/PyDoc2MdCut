"""纯文本转换器：UTF-8 优先，其他编码自动探测。"""
from __future__ import annotations

import logging
from pathlib import Path

from charset_normalizer import from_bytes

from .base import BaseConverter, ConversionResult

logger = logging.getLogger(__name__)


class TxtConverter(BaseConverter):
    kind = "txt"

    def convert(self, path: Path) -> ConversionResult:
        try:
            raw = path.read_bytes()
        except OSError as e:
            return ConversionResult(
                status="failed",
                markdown="",
                error=f"读取失败: {e.__class__.__name__}: {e}",
            )

        # pre-read 截取（避免后续解码 / 渲染处理超大文件）
        raw, _pre_info, pre_warnings = self._pre_read_truncate_bytes(raw, path=path)

        # UTF-8 优先
        text: str | None = None
        encoding = "utf-8"
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            best = from_bytes(raw).best()
            if best is not None:
                text = str(best)
                encoding = best.encoding or "unknown"
            else:
                # 兜底：errors=replace，至少不丢内容
                text = raw.decode("utf-8", errors="replace")
                encoding = "utf-8(replace)"

        warnings: list[str] = list(pre_warnings)
        if encoding not in ("utf-8", "utf_8", "ascii"):
            warnings.append(f"原文件编码非 UTF-8（探测为 {encoding}），已转码")

        # 用 fenced 代码块包起来，避免文本里的 markdown 语法影响合并 MD 的渲染
        md = f"```text\n{text}\n```\n"
        return ConversionResult(
            status="success",
            markdown=md,
            warnings=warnings,
            extra={"chars": len(text), "encoding": encoding},
        )
