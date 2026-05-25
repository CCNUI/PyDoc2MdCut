"""PowerPoint 97-2003 (.ppt / .pot / .pps) → markdown,走 olefile 粗略文本抽取。

注意:.ppt 是 OLE 复合文档,正文存在 "PowerPoint Document" 流里,
olefile 能读出原始字节,但 PPT 内部用复杂的"原子记录"格式存幻灯片元素,
没法重建幻灯片顺序/版式/形状位置。能抽出来的只是文本碎片。
"""
from __future__ import annotations

from pathlib import Path

from ._legacy_office import convert_legacy_office
from .base import BaseConverter, ConversionResult


class PptConverter(BaseConverter):
    kind = "ppt"

    def convert(self, path: Path) -> ConversionResult:
        return convert_legacy_office(
            path,
            file_label="PowerPoint 97-2003 演示文稿 (.ppt)",
            new_format=".pptx",
        )
