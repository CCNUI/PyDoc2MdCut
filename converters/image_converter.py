"""图片 → 通过百度 OCR 识别为文本。

行为：
- OCR 未启用 → status='skipped'，原因 "OCR 未启用"
- 图片超 4MB → status='skipped'，原因 "图片超过 OCR_MAX_IMAGE_BYTES"
- OCR 调用失败（重试用尽）→ status='failed'，error 含百度错误码（如有）
"""
from __future__ import annotations

import logging
from pathlib import Path

from config import AppConfig
from ocr.baidu import BaiduOCRClient, OCRError, OCRResult

from .base import BaseConverter, ConversionResult

logger = logging.getLogger(__name__)

_ENGINE_NAME_CN = {
    "accurate_basic": "百度文字识别（高精度版）",
    "general_basic": "百度文字识别（标准版）",
}


class ImageConverter(BaseConverter):
    """注意：构造时需要传入 ocr_client（可以为 None，表示 OCR 未启用）。"""

    kind = "image"

    def __init__(self, cfg: AppConfig, ocr_client: BaiduOCRClient | None) -> None:
        super().__init__(cfg)
        self.ocr_client = ocr_client

    def convert(self, path: Path) -> ConversionResult:
        try:
            size = path.stat().st_size
        except OSError as e:
            return ConversionResult(
                status="failed",
                markdown="",
                error=f"读取图片失败: {e.__class__.__name__}: {e}",
            )

        # OCR 未启用 → skipped
        if self.ocr_client is None:
            md = (
                "> 🚫 **图片已跳过（OCR 未启用）**\n"
                f"> - 文件: {path.name}\n"
                f"> - 大小: {_human_bytes(size)}\n"
                "> - 提示: 在 .env 中设置 OCR_ENABLED=true 并填入百度 API 凭据后即可识别。\n"
            )
            return ConversionResult(
                status="skipped",
                markdown=md,
                skip_reason="OCR 未启用",
                extra={"size": size},
            )

        # 超大图片 → skipped
        if size > self.cfg.ocr_max_image_bytes:
            md = (
                "> 🚫 **图片已跳过（超过 OCR 体积上限）**\n"
                f"> - 文件: {path.name}\n"
                f"> - 大小: {_human_bytes(size)}（> "
                f"{_human_bytes(self.cfg.ocr_max_image_bytes)}）\n"
                "> - 百度 OCR 单图限制 4MB，请压缩或裁切后重试。\n"
            )
            return ConversionResult(
                status="skipped",
                markdown=md,
                skip_reason="图片超过 OCR 体积上限",
                extra={"size": size},
            )

        # 调 OCR
        width, height = _try_get_image_size(path)
        try:
            result: OCRResult = self.ocr_client.recognize_image(path)
        except OCRError as e:
            err_msg = str(e)
            code = e.error_code
            md = (
                "> ❌ **此图片 OCR 失败**\n"
                f"> - 文件: {path.name}\n"
                f"> - 大小: {_human_bytes(size)}\n"
                + (f"> - 百度错误码: {code}\n" if code is not None else "")
                + f"> - 异常: {err_msg}\n"
            )
            return ConversionResult(
                status="failed",
                markdown=md,
                error=f"OCR 失败{f' (code={code})' if code else ''}: {err_msg}",
                extra={"size": size, "ocr_error_code": code},
            )

        engine_label = _ENGINE_NAME_CN.get(result.engine, result.engine)

        # 成功
        header_lines = [
            "> 📷 **图片 OCR 结果**",
            f"> - 文件: {path.name}",
            f"> - 尺寸: {width}×{height}" if (width and height) else "> - 尺寸: 未知",
            f"> - 大小: {_human_bytes(size)}",
            f"> - OCR 引擎: {engine_label}",
            f"> - 识别字符数: {result.char_count}",
            f"> - 识别耗时: {result.elapsed_seconds:.2f}s",
        ]
        md_parts = ["\n".join(header_lines), "\n\n"]
        if result.text.strip():
            md_parts.append("```\n")
            md_parts.append(result.text)
            if not result.text.endswith("\n"):
                md_parts.append("\n")
            md_parts.append("```\n")
        else:
            md_parts.append("> ⚠️ OCR 未识别到任何文字（可能是纯色/纯图/极低分辨率）\n")

        return ConversionResult(
            status="success",
            markdown="".join(md_parts),
            extra={
                "size": size,
                "ocr_chars": result.char_count,
                "ocr_elapsed": result.elapsed_seconds,
                "ocr_engine": result.engine,
                "ocr_lines": result.raw_lines,
                "width": width,
                "height": height,
            },
        )


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    if n < 1024 * 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    return f"{n / (1024 * 1024 * 1024):.2f} GB"


def _try_get_image_size(path: Path) -> tuple[int, int]:
    """尝试读图片尺寸；不强依赖 PIL（因为 requirements.txt 没列），失败返回 (0,0)。"""
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as im:
            return int(im.width), int(im.height)
    except Exception:
        return 0, 0
