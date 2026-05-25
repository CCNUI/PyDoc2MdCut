"""Excel 97-2003 (.xls / .xlt) → markdown,走 olefile 粗略文本抽取。

注意:.xls 是 BIFF 二进制格式,olefile 只能抽出工作簿流里的可打印字符串。
单元格的行/列位置、公式、格式都拿不到。如果你需要保留这些,
请用 WPS / Excel 另存为 .xlsx,或装 LibreOffice。
"""
from __future__ import annotations

from pathlib import Path

from ._legacy_office import convert_legacy_office
from .base import BaseConverter, ConversionResult


class XlsConverter(BaseConverter):
    kind = "xls"

    def convert(self, path: Path) -> ConversionResult:
        return convert_legacy_office(
            path,
            file_label="Excel 97-2003 工作簿 (.xls)",
            new_format=".xlsx",
        )
