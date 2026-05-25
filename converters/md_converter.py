"""Markdown 转换器：原样保留。"""
from __future__ import annotations

from pathlib import Path

from charset_normalizer import from_bytes

from .base import BaseConverter, ConversionResult


class MarkdownConverter(BaseConverter):
    kind = "markdown"

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

        try:
            text = raw.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            best = from_bytes(raw).best()
            if best is not None:
                text = str(best)
                encoding = best.encoding or "unknown"
            else:
                text = raw.decode("utf-8", errors="replace")
                encoding = "utf-8(replace)"

        # 不包代码块（保留 markdown 语义）
        # 但末尾确保有换行
        if not text.endswith("\n"):
            text += "\n"
        return ConversionResult(
            status="success",
            markdown=text,
            warnings=list(pre_warnings),
            extra={"chars": len(text), "encoding": encoding},
        )
