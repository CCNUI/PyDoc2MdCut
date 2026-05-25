"""PDF → Markdown，使用 pdfplumber。

包含图片型 PDF 检测（字符数/页数 < 阈值）。
"""
from __future__ import annotations

import logging
from pathlib import Path

import pdfplumber

from .base import BaseConverter, ConversionResult

logger = logging.getLogger(__name__)


class PdfConverter(BaseConverter):
    kind = "pdf"

    def convert(self, path: Path) -> ConversionResult:
        try:
            with pdfplumber.open(str(path)) as pdf:
                page_count = len(pdf.pages)
                page_texts: list[str] = []
                for i, page in enumerate(pdf.pages, start=1):
                    try:
                        text = page.extract_text() or ""
                    except Exception as e:
                        logger.debug(f"PDF page {i} 提取失败: {e}")
                        text = ""
                    page_texts.append(text)
        except Exception as e:
            return ConversionResult(
                status="failed",
                markdown="",
                error=f"PDF 打开/解析失败: {e.__class__.__name__}: {e}",
            )

        full_text = "\n\n".join(page_texts)
        total_chars = len(full_text.strip())
        warnings: list[str] = []
        is_scanned = False

        if page_count > 0:
            chars_per_page = total_chars / page_count
            if chars_per_page < self.cfg.scanned_pdf_char_per_page:
                is_scanned = True
                warnings.append(
                    f"图片型 PDF 嫌疑：平均每页字符数 {chars_per_page:.1f} "
                    f"< 阈值 {self.cfg.scanned_pdf_char_per_page}"
                )

        # 拼装 markdown
        parts: list[str] = []
        if is_scanned:
            parts.append(
                "> ⚠️ **警告：检测到此 PDF 可能为扫描件/图片型 PDF**\n"
                "> 自动文本提取可能不完整或为空。建议使用 OCR 工具后再转换。\n\n"
            )
        parts.append(f"- 页数: {page_count}\n- 提取字符数: {total_chars}\n\n")
        for i, text in enumerate(page_texts, start=1):
            parts.append(f"### Page {i}\n\n")
            if text.strip():
                # 用代码块包，避免 PDF 中表格类内容破坏 markdown 结构
                # 但纯文本内容完整保留
                parts.append(text.strip() + "\n\n")
            else:
                parts.append("_（本页未提取到文本）_\n\n")

        md = "".join(parts)
        return ConversionResult(
            status="success",
            markdown=md,
            warnings=warnings,
            extra={
                "chars": total_chars,
                "pages": page_count,
                "is_scanned_pdf": is_scanned,
            },
        )
