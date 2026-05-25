"""JSON 转换器：格式化后包进 ```json 代码块。"""
from __future__ import annotations

import json
from pathlib import Path

from charset_normalizer import from_bytes

from .base import BaseConverter, ConversionResult


class JsonConverter(BaseConverter):
    kind = "json"

    def convert(self, path: Path) -> ConversionResult:
        try:
            raw = path.read_bytes()
        except OSError as e:
            return ConversionResult(
                status="failed",
                markdown="",
                error=f"读取失败: {e.__class__.__name__}: {e}",
            )

        # pre-read 截取（JSON 解析对大文件昂贵；截了之后通常解析会失败，
        # 但代码已经会兜底为「按文本原样输出」并加警告，体验仍可接受）
        raw, _info, pre_warnings = self._pre_read_truncate_bytes(raw, path=path)

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            best = from_bytes(raw).best()
            text = str(best) if best is not None else raw.decode("utf-8", errors="replace")

        warnings: list[str] = list(pre_warnings)
        try:
            obj = json.loads(text)
            pretty = json.dumps(obj, ensure_ascii=False, indent=2)
        except json.JSONDecodeError as e:
            # 不能解析也不算失败：包成 text 代码块、给警告
            if pre_warnings:
                warnings.append(
                    f"JSON 已被 pre-read 截取，解析失败（{e.msg}）属预期，按文本原样输出"
                )
            else:
                warnings.append(
                    f"JSON 解析失败（{e.msg} 行{e.lineno} 列{e.colno}），按文本原样输出"
                )
            pretty = text

        md = f"```json\n{pretty}\n```\n"
        return ConversionResult(
            status="success",
            markdown=md,
            warnings=warnings,
            extra={"chars": len(pretty)},
        )
