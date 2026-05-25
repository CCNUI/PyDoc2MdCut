"""CSV → Markdown 表格。超过 CSV_MAX_ROWS 行截断。"""
from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from .base import BaseConverter, ConversionResult

logger = logging.getLogger(__name__)


class CsvConverter(BaseConverter):
    kind = "csv"

    def convert(self, path: Path) -> ConversionResult:
        warnings: list[str] = []
        df: pd.DataFrame | None = None
        last_err: Exception | None = None

        # pre-read 截取：仅当 catalog 为 CSV 启用了截取时才进入这个分支；
        # 否则直接让 pandas 读文件（保持原行为，速度最快）。
        # 注意：截过的 CSV 中间会插入「... TRUNCATED ...」标记，pandas 解析时大概率会被
        # `on_bad_lines='skip'` 跳过；header 仍在头部，因而表格仍然能正确成形。
        pre_warnings: list[str] = []
        used_pre_read = False
        from io import BytesIO
        catalog = getattr(self.cfg, "truncation_catalog", None)
        if (
            catalog is not None
            and getattr(self.cfg, "truncation_enabled", False)
        ):
            spec = catalog.spec_for(ext=path.suffix.lower(), kind=self.kind)
            if spec.is_meaningful():
                try:
                    raw = path.read_bytes()
                except OSError as e:
                    return ConversionResult(
                        status="failed", markdown="",
                        error=f"读取失败: {e.__class__.__name__}: {e}",
                    )
                raw, _info, pre_warnings = self._pre_read_truncate_bytes(raw, path=path)
                used_pre_read = bool(pre_warnings)
                # 编码 fallback：utf-8 / utf-8-sig / gbk / latin-1
                for encoding in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
                    try:
                        df = pd.read_csv(
                            BytesIO(raw), encoding=encoding, dtype=str,
                            keep_default_na=False, engine="python",
                            on_bad_lines="skip",
                        )
                        if encoding != "utf-8":
                            warnings.append(f"CSV 用 {encoding} 编码读取（utf-8 失败）")
                        break
                    except UnicodeDecodeError as e:
                        last_err = e
                        continue
                    except Exception as e:
                        last_err = e
                        continue

        if df is None:
            # 走原始路径：直接让 pandas 读文件
            # 编码 fallback：utf-8 / utf-8-sig / gbk / latin-1
            for encoding in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
                try:
                    df = pd.read_csv(path, encoding=encoding, dtype=str, keep_default_na=False)
                    if encoding != "utf-8":
                        warnings.append(f"CSV 用 {encoding} 编码读取（utf-8 失败）")
                    break
                except UnicodeDecodeError as e:
                    last_err = e
                    continue
                except (pd.errors.ParserError, pd.errors.EmptyDataError) as e:
                    last_err = e
                    # parser 错误下尝试 python 引擎宽松模式
                    try:
                        df = pd.read_csv(
                            path, encoding=encoding, dtype=str,
                            keep_default_na=False, engine="python",
                            on_bad_lines="skip",
                        )
                        warnings.append(f"CSV 解析有坏行已跳过（{e.__class__.__name__}）")
                        break
                    except Exception as e2:
                        last_err = e2
                        continue
                except Exception as e:
                    last_err = e
                    continue

        if df is None:
            return ConversionResult(
                status="failed",
                markdown="",
                error=f"CSV 读取失败: {last_err.__class__.__name__ if last_err else 'Unknown'}: {last_err}",
            )

        if used_pre_read:
            warnings = pre_warnings + warnings

        total_rows = len(df)
        truncated = False
        if total_rows > self.cfg.csv_max_rows:
            df = df.head(self.cfg.csv_max_rows)
            truncated = True
            warnings.append(
                f"CSV 行数 {total_rows} 超过上限 {self.cfg.csv_max_rows}，已截断"
            )

        # to_markdown 需要 tabulate 包，用不到的话手写更稳；这里手写避免新增依赖
        md_table = _df_to_markdown_table(df)
        header = (
            f"- 总行数: {total_rows}（"
            f"{'已截断到 ' + str(self.cfg.csv_max_rows) + ' 行' if truncated else '全部展示'}）\n"
            f"- 列数: {len(df.columns)}\n\n"
        )

        return ConversionResult(
            status="success",
            markdown=header + md_table + "\n",
            warnings=warnings,
            extra={
                "chars": len(md_table),
                "rows": total_rows,
                "truncated": truncated,
                "cols": len(df.columns),
            },
        )


def _escape_md_cell(s: str) -> str:
    """转义 markdown 表格单元格内容：管道、换行。"""
    if s is None:
        return ""
    return (
        str(s)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r\n", " ")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def _df_to_markdown_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_（空表）_\n"
    headers = [str(c) for c in df.columns]
    lines = [
        "| " + " | ".join(_escape_md_cell(h) for h in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for _, row in df.iterrows():
        lines.append("| " + " | ".join(_escape_md_cell(v) for v in row.tolist()) + " |")
    return "\n".join(lines) + "\n"
