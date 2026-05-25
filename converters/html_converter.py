"""HTML → Markdown，使用 markdownify。"""
from __future__ import annotations

from pathlib import Path

from charset_normalizer import from_bytes
from markdownify import markdownify

from .base import BaseConverter, ConversionResult


class HtmlConverter(BaseConverter):
    kind = "html"

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
            html = raw.decode("utf-8")
        except UnicodeDecodeError:
            best = from_bytes(raw).best()
            html = str(best) if best is not None else raw.decode("utf-8", errors="replace")

        try:
            md = markdownify(html, heading_style="ATX")
        except Exception as e:
            return ConversionResult(
                status="failed",
                markdown="",
                error=f"HTML→Markdown 转换失败: {e.__class__.__name__}: {e}",
            )
        if not md.endswith("\n"):
            md += "\n"
        return ConversionResult(
            status="success", markdown=md, warnings=list(pre_warnings),
            extra={"chars": len(md)},
        )
