"""Excel → Markdown：每个 sheet 一个二级标题 + 表格。"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from .base import BaseConverter, ConversionResult
from .csv_converter import _df_to_markdown_table  # 复用

logger = logging.getLogger(__name__)


class XlsxConverter(BaseConverter):
    kind = "xlsx"

    def convert(self, path: Path) -> ConversionResult:
        try:
            # 用 openpyxl 引擎；keep_default_na=False 让 'NA' 不被当 NaN
            xls = pd.read_excel(
                path, sheet_name=None, dtype=str,
                keep_default_na=False, engine="openpyxl",
            )
        except Exception as e:
            return ConversionResult(
                status="failed",
                markdown="",
                error=f"Excel 读取失败: {e.__class__.__name__}: {e}",
            )

        if not xls:
            return ConversionResult(
                status="success",
                markdown="_（工作簿为空）_\n",
                extra={"chars": 0, "sheets": 0},
            )

        warnings: list[str] = []
        parts: list[str] = []
        total_rows = 0
        for sheet_name, df in xls.items():
            parts.append(f"## Sheet: {sheet_name}\n")
            sheet_total = len(df)
            total_rows += sheet_total
            truncated = False
            if sheet_total > self.cfg.csv_max_rows:
                df = df.head(self.cfg.csv_max_rows)
                truncated = True
                warnings.append(
                    f"Sheet '{sheet_name}' 行数 {sheet_total} 超过上限 "
                    f"{self.cfg.csv_max_rows}，已截断"
                )
            parts.append(
                f"- 总行数: {sheet_total}"
                + (f"（截断到 {self.cfg.csv_max_rows}）" if truncated else "")
                + f"  列数: {len(df.columns)}\n\n"
            )
            parts.append(_df_to_markdown_table(df))
            parts.append("\n")

        md = "".join(parts)
        return ConversionResult(
            status="success",
            markdown=md,
            warnings=warnings,
            extra={"chars": len(md), "sheets": len(xls), "rows": total_rows},
        )
