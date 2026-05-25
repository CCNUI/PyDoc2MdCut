"""Word 97-2003 (.doc / .dot) → markdown,走 olefile 粗略文本抽取。"""
from __future__ import annotations

from pathlib import Path

from ._legacy_office import convert_legacy_office
from .base import BaseConverter, ConversionResult


class DocConverter(BaseConverter):
    """旧版 Word: 仅 olefile 兜底。完整渲染需要装 LibreOffice (见 wps_converter)。"""

    kind = "doc"

    def convert(self, path: Path) -> ConversionResult:
        return convert_legacy_office(
            path,
            file_label="Word 97-2003 文档 (.doc)",
            new_format=".docx",
        )
