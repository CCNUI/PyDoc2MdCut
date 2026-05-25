"""通用文本日志 (.log) 转换器。

行为：
- UTF-8 优先；探测失败回落
- 体积超过 LOG_FILE_MAX_BYTES → 走"头+尾"截断（中间舍弃）
- 整体包进 ```log 代码块
- 极端情况下文件实际是二进制（如某些工程把 binlog 也叫 .log）→ 探测后给警告并尝试以
  errors='replace' 解码。这种情况报告里会有警告说明。
"""
from __future__ import annotations

import logging
from pathlib import Path

from charset_normalizer import from_bytes

from ._log_utils import truncate_text_bytes
from .base import BaseConverter, ConversionResult

logger = logging.getLogger(__name__)


def _is_likely_binary(raw: bytes, sample_size: int = 4096) -> bool:
    """启发式判断：前 sample_size 字节里 NUL 多 / 不可打印多 → 视为二进制。"""
    sample = raw[:sample_size]
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    # 计算 ASCII 可打印 / 控制字符比例
    printable = sum(
        1 for b in sample
        if (32 <= b < 127) or b in (9, 10, 13)
    )
    return (printable / len(sample)) < 0.7


def _decode(raw: bytes) -> tuple[str, str, list[str]]:
    """返回 (text, encoding, warnings)。"""
    warnings: list[str] = []
    if _is_likely_binary(raw):
        warnings.append(
            ".log 看起来是二进制内容（含 NUL 或非可打印字符比例较高），"
            "已尽力解码，可能有损"
        )
        try:
            return raw.decode("utf-8", errors="replace"), "utf-8(replace)", warnings
        except Exception:
            return raw.decode("latin-1", errors="replace"), "latin-1(replace)", warnings

    try:
        return raw.decode("utf-8"), "utf-8", warnings
    except UnicodeDecodeError:
        best = from_bytes(raw).best()
        if best is not None:
            enc = best.encoding or "unknown"
            warnings.append(f"原文件编码非 UTF-8（探测为 {enc}），已转码")
            return str(best), enc, warnings
        warnings.append("无法识别编码，使用 utf-8(replace) 兜底")
        return raw.decode("utf-8", errors="replace"), "utf-8(replace)", warnings


class LogConverter(BaseConverter):
    """通用文本日志。"""

    kind = "log"

    def convert(self, path: Path) -> ConversionResult:
        try:
            raw = path.read_bytes()
        except OSError as e:
            return ConversionResult(
                status="failed",
                markdown="",
                error=f"读取失败: {e.__class__.__name__}: {e}",
            )

        # ---- 新机制：pre-read 截取（按 .env 中的 TRUNC_KIND_LOG_* / TRUNC_EXT_LOG_* 配置）----
        raw_after_new, pre_info, pre_warnings = self._pre_read_truncate_bytes(raw, path=path)

        # ---- 旧机制兜底：LOG_FILE_MAX_BYTES（只有在新机制未触发截取且仍超限时才用） ----
        if not pre_info.truncated and len(raw_after_new) > self.cfg.log_file_max_bytes:
            truncated_bytes, info = truncate_text_bytes(
                raw_after_new,
                max_bytes=self.cfg.log_file_max_bytes,
                head_ratio=self.cfg.log_truncate_head_ratio,
            )
        else:
            truncated_bytes = raw_after_new
            # 构造一个未触发的 info 占位
            class _Info:
                truncated = pre_info.truncated
                original_bytes = pre_info.original_bytes
                kept_bytes = pre_info.kept_bytes
                head_bytes = pre_info.head_bytes
                tail_bytes = pre_info.tail_bytes
                skipped_bytes = pre_info.skipped_bytes
                def summary(self):
                    return pre_info.summary()
            info = _Info()

        # 2) 解码
        text, encoding, warnings = _decode(truncated_bytes)
        warnings = list(pre_warnings) + warnings

        if info.truncated and not pre_info.truncated:
            warnings.append(info.summary())

        header = (
            "**通用文本日志**\n\n"
            f"- 原始大小: {info.original_bytes:,} bytes\n"
            f"- 文件编码: {encoding}\n"
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
                "original_bytes": info.original_bytes,
                "truncated": info.truncated,
                "kept_bytes": info.kept_bytes,
                "head_bytes": info.head_bytes,
                "tail_bytes": info.tail_bytes,
                "skipped_bytes": info.skipped_bytes,
            },
        )
