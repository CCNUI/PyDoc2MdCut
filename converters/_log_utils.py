"""日志类（.log .tlog .rlog 文本部分）共用的体积截断工具。

策略：当原始字节数超过 LOG_FILE_MAX_BYTES 时，保留头部 + 尾部，
中间用一段醒目的 `...TRUNCATED M bytes...` 标记替代。

切分点严格落在 UTF-8 字符边界以及行边界（\n）上，避免破坏内容。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TruncationInfo:
    """供报告/合并 MD 使用的截断元信息。"""
    truncated: bool
    original_bytes: int
    kept_bytes: int
    head_bytes: int
    tail_bytes: int
    skipped_bytes: int

    def summary(self) -> str:
        if not self.truncated:
            return f"未截断（原始 {self.original_bytes:,} 字节）"
        return (
            f"已截断：原始 {self.original_bytes:,} → 保留 {self.kept_bytes:,} 字节"
            f"（头 {self.head_bytes:,} + 尾 {self.tail_bytes:,}，"
            f"中间舍弃 {self.skipped_bytes:,} 字节）"
        )


def _safe_utf8_back(raw: bytes, idx: int) -> int:
    """把 idx 调到 UTF-8 字符边界（向左收缩）。"""
    i = min(idx, len(raw))
    while i > 0 and (raw[i] & 0xC0) == 0x80:
        i -= 1
    return i


def _safe_utf8_forward(raw: bytes, idx: int) -> int:
    """把 idx 调到 UTF-8 字符边界（向右扩展）。"""
    i = max(idx, 0)
    while i < len(raw) and (raw[i] & 0xC0) == 0x80:
        i += 1
    return i


def truncate_text_bytes(
    raw: bytes,
    *,
    max_bytes: int,
    head_ratio: float = 0.5,
) -> tuple[bytes, TruncationInfo]:
    """对 raw 做"头+尾"截断。

    Args:
        raw: 原始字节流（一般是已读入内存的文本字节）
        max_bytes: 期望保留的最大字节（不含中间标记本身）
        head_ratio: 头部占多少比例（0~1，剩下给尾部）

    Returns:
        (truncated_bytes, info)
        truncated_bytes 中已经插入了一段 `\\n...TRUNCATED N bytes...\\n` 标记。
    """
    n = len(raw)
    if n <= max_bytes:
        return raw, TruncationInfo(
            truncated=False, original_bytes=n, kept_bytes=n,
            head_bytes=n, tail_bytes=0, skipped_bytes=0,
        )

    head_target = max(1, int(max_bytes * head_ratio))
    tail_target = max(1, max_bytes - head_target)

    # 头部：从 0 开始，向后取 head_target 字节，落在行边界 + UTF-8 边界
    head_end_raw = head_target
    head_end = raw.rfind(b"\n", 0, head_end_raw)
    if head_end == -1 or head_end < head_target // 2:
        # 没找到合适换行，就回退到 UTF-8 边界硬切
        head_end = _safe_utf8_back(raw, head_end_raw)
    else:
        head_end += 1  # 包含换行符

    # 尾部：从末尾向前取 tail_target 字节，落在行边界 + UTF-8 边界
    tail_start_raw = max(head_end, n - tail_target)
    # 在 [tail_start_raw, n] 里找第一个 \n 之后开始
    nl = raw.find(b"\n", tail_start_raw, n)
    if nl != -1:
        tail_start = nl + 1
    else:
        tail_start = _safe_utf8_forward(raw, tail_start_raw)

    if tail_start <= head_end:
        # 重叠，干脆只保留头部
        tail_start = n
        head_end = min(head_end, n)

    skipped = max(0, tail_start - head_end)
    head_part = raw[:head_end]
    tail_part = raw[tail_start:]

    marker = (
        f"\n\n... TRUNCATED {skipped:,} bytes "
        f"(LOG_FILE_MAX_BYTES = {max_bytes:,}) ...\n\n"
    ).encode("utf-8")

    out = head_part + marker + tail_part
    info = TruncationInfo(
        truncated=True,
        original_bytes=n,
        kept_bytes=len(head_part) + len(tail_part),
        head_bytes=len(head_part),
        tail_bytes=len(tail_part),
        skipped_bytes=skipped,
    )
    return out, info
