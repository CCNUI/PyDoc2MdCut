"""rlog 转换器（ROS bag-style / ArduPilot raw log / 通用 raw log）。

策略：
- 探测前 4 KiB：若文本占比足够高 → 当文本日志处理（受 LOG_FILE_MAX_BYTES 截取）
- 否则视为二进制 → 输出元信息 + 头部 hex 预览（前 256 bytes）+ 尾部 hex 预览（最后 256 bytes）
  二进制版本不入 LLM 主流（信息量低且占字数）；用户若要解析需要专用工具。

注意：ArduPilot 的真正 dataflash 日志通常扩展名为 .BIN/.bin/.ulog，但社区也常把
"raw log" 文件命名为 .rlog；ROS 的 rosbag2 默认扩展名是 .db3 或 .mcap，但旧的 ROS1
bag 也偶有 .rlog 写法。我们做"无侵入式"的二进制摘要，不强求语义解码。
"""
from __future__ import annotations

import binascii
import logging
from pathlib import Path

from ._log_utils import truncate_text_bytes
from .base import BaseConverter, ConversionResult
from .log_converter import _decode, _is_likely_binary

logger = logging.getLogger(__name__)


def _hex_preview(data: bytes, *, bytes_per_line: int = 16) -> str:
    """生成可读的 hex 预览：偏移量 + hex + ASCII。"""
    if not data:
        return "(空)\n"
    lines = []
    for offset in range(0, len(data), bytes_per_line):
        chunk = data[offset : offset + bytes_per_line]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(
            chr(b) if 32 <= b < 127 else "." for b in chunk
        )
        # 头部对齐
        lines.append(f"{offset:08x}  {hex_part:<{bytes_per_line * 3 - 1}}  |{ascii_part}|")
    return "\n".join(lines) + "\n"


class RlogConverter(BaseConverter):
    """rlog 文件（自动判定文本/二进制）。"""

    kind = "rlog"

    def convert(self, path: Path) -> ConversionResult:
        try:
            raw = path.read_bytes()
        except OSError as e:
            return ConversionResult(
                status="failed",
                markdown="",
                error=f"读取失败: {e.__class__.__name__}: {e}",
            )

        warnings: list[str] = []

        if _is_likely_binary(raw):
            return self._convert_binary(path, raw)
        return self._convert_text(raw, path=path)

    def _convert_text(self, raw: bytes, *, path: Path) -> ConversionResult:
        # 新机制：pre-read 截取
        raw_after_new, pre_info, pre_warnings = self._pre_read_truncate_bytes(raw, path=path)

        # 旧机制兜底
        if not pre_info.truncated and len(raw_after_new) > self.cfg.log_file_max_bytes:
            truncated_bytes, info = truncate_text_bytes(
                raw_after_new,
                max_bytes=self.cfg.log_file_max_bytes,
                head_ratio=self.cfg.log_truncate_head_ratio,
            )
        else:
            truncated_bytes = raw_after_new
            info = type("I", (), {
                "truncated": pre_info.truncated,
                "original_bytes": pre_info.original_bytes,
                "kept_bytes": pre_info.kept_bytes,
                "head_bytes": pre_info.head_bytes,
                "tail_bytes": pre_info.tail_bytes,
                "skipped_bytes": pre_info.skipped_bytes,
                "summary": (lambda self: pre_info.summary()),
            })()

        text, encoding, decode_warnings = _decode(truncated_bytes)
        warnings = list(pre_warnings) + decode_warnings
        if info.truncated and not pre_info.truncated:
            warnings.append(info.summary())

        header = (
            "**rlog 文本模式**\n\n"
            f"- 原始大小: {info.original_bytes:,} bytes\n"
            f"- 探测结果: 文本（按文本日志处理）\n"
            f"- 编码: {encoding}\n"
            f"- 截断: {'是' if info.truncated else '否'}\n"
        )
        if info.truncated:
            header += (
                f"- 头部保留: {info.head_bytes:,} bytes\n"
                f"- 尾部保留: {info.tail_bytes:,} bytes\n"
                f"- 中间舍弃: {info.skipped_bytes:,} bytes\n"
            )
        header += "\n"

        body = "```log\n" + text + ("\n" if not text.endswith("\n") else "") + "```\n"
        return ConversionResult(
            status="success",
            markdown=header + body,
            warnings=warnings,
            extra={
                "chars": len(text),
                "encoding": encoding,
                "is_binary": False,
                "original_bytes": info.original_bytes,
                "truncated": info.truncated,
                "kept_bytes": info.kept_bytes,
            },
        )

    def _convert_binary(self, path: Path, raw: bytes) -> ConversionResult:
        size = len(raw)
        head_n = min(256, size)
        tail_n = min(256, max(0, size - head_n))
        head = raw[:head_n]
        tail = raw[size - tail_n :] if tail_n > 0 else b""

        # SHA-1 摘要（前 16 字节做指纹用）
        try:
            import hashlib
            sha1 = hashlib.sha1(raw).hexdigest()
        except Exception:
            sha1 = "(计算失败)"

        warnings = [
            "rlog 文件为二进制内容；本工具不做语义解码，"
            "只输出体积/指纹/hex 预览。需要解码请用专用工具（如 pymavlink、rosbag）"
        ]

        md_parts = [
            "**rlog 二进制模式**\n\n",
            f"- 文件大小: {size:,} bytes\n",
            f"- SHA-1: `{sha1}`\n",
            f"- 头部预览: {head_n} bytes\n",
            f"- 尾部预览: {tail_n} bytes\n\n",
            "### 头部 hex\n\n",
            "```\n",
            _hex_preview(head),
            "```\n\n",
        ]
        if tail_n > 0:
            md_parts += [
                "### 尾部 hex\n\n",
                "```\n",
                _hex_preview(tail),
                "```\n",
            ]

        return ConversionResult(
            status="success",
            markdown="".join(md_parts),
            warnings=warnings,
            extra={
                "chars": 0,
                "is_binary": True,
                "original_bytes": size,
                "sha1": sha1,
                "head_bytes": head_n,
                "tail_bytes": tail_n,
            },
        )
