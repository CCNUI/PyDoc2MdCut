"""tlog (MAVLink 遥测日志) 转换器。

策略：
- pymavlink 已安装 → 解析每条 MAVLink 消息，输出按消息类型聚合的统计 + 头部样本消息
- pymavlink 未安装 → 输出元信息 + hex 预览 + 提示用户安装 pymavlink 获得更佳结果

不论哪种路径，都会受 LOG_FILE_MAX_BYTES 影响：
- 文本输出（解码后的消息列表）超过限额 → 走"头+尾"截断
- pymavlink 解析时会限制最多解析 N 条消息（避免大 tlog 撑爆内存）

注意：tlog 是裸 MAVLink 字节流（v1 / v2 都有），通常每条消息前是
一个 8 字节的 unix 微秒时间戳（little-endian），但也有不带时间戳的变体。
pymavlink 的 mavutil.mavlink_connection() 会自动处理这两种。
"""
from __future__ import annotations

import binascii
import importlib.util
import logging
from pathlib import Path

from ._log_utils import truncate_text_bytes
from .base import BaseConverter, ConversionResult

logger = logging.getLogger(__name__)


# 单次最多解析多少条 MAVLink 消息；超过就停（避免巨型 tlog 跑爆内存）
_MAX_MAVLINK_MESSAGES = 200_000
# 报告里"样本消息"列举多少条
_SAMPLE_HEAD_MESSAGES = 50
_SAMPLE_TAIL_MESSAGES = 20


def _has_pymavlink() -> bool:
    return importlib.util.find_spec("pymavlink") is not None


def _hex_preview(data: bytes, *, bytes_per_line: int = 16) -> str:
    if not data:
        return "(空)\n"
    lines = []
    for offset in range(0, len(data), bytes_per_line):
        chunk = data[offset : offset + bytes_per_line]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{offset:08x}  {hex_part:<{bytes_per_line * 3 - 1}}  |{ascii_part}|")
    return "\n".join(lines) + "\n"


class TlogConverter(BaseConverter):
    """MAVLink 遥测日志。"""

    kind = "tlog"

    def convert(self, path: Path) -> ConversionResult:
        try:
            size = path.stat().st_size
        except OSError as e:
            return ConversionResult(
                status="failed",
                markdown="",
                error=f"读取失败: {e.__class__.__name__}: {e}",
            )

        if _has_pymavlink():
            return self._convert_with_pymavlink(path, size)
        return self._convert_metadata_only(path, size)

    # ---------- pymavlink 路径 ----------
    def _convert_with_pymavlink(self, path: Path, size: int) -> ConversionResult:
        try:
            from pymavlink import mavutil  # type: ignore
        except Exception as e:
            logger.warning(f"导入 pymavlink 失败，回落到元信息模式: {e}")
            return self._convert_metadata_only(path, size, extra_warning=str(e))

        warnings: list[str] = []
        msg_counts: dict[str, int] = {}
        sample_head: list[str] = []
        sample_tail: list[str] = []
        first_ts: float | None = None
        last_ts: float | None = None
        n_messages = 0
        capped = False

        try:
            mlog = mavutil.mavlink_connection(str(path), notimestamps=False, robust_parsing=True)
        except Exception as e:
            warnings.append(f"pymavlink 打开失败：{e}；回落到元信息模式")
            return self._convert_metadata_only(path, size, extra_warning=str(e))

        try:
            while True:
                if n_messages >= _MAX_MAVLINK_MESSAGES:
                    capped = True
                    break
                try:
                    m = mlog.recv_match(blocking=False)
                except Exception as e:
                    warnings.append(f"解析中遇到异常（已跳过）：{e.__class__.__name__}: {e}")
                    break
                if m is None:
                    break

                n_messages += 1
                t = getattr(m, "_timestamp", None)
                if t is not None:
                    if first_ts is None:
                        first_ts = float(t)
                    last_ts = float(t)

                mtype = m.get_type()
                msg_counts[mtype] = msg_counts.get(mtype, 0) + 1

                # 头部 / 尾部样本（尾部用滚动）
                if len(sample_head) < _SAMPLE_HEAD_MESSAGES:
                    sample_head.append(self._format_message(m, t))
                else:
                    sample_tail.append(self._format_message(m, t))
                    if len(sample_tail) > _SAMPLE_TAIL_MESSAGES:
                        sample_tail.pop(0)
        except Exception as e:
            warnings.append(f"pymavlink 解析未完成（{e.__class__.__name__}: {e}）")

        # 拼装 markdown
        parts: list[str] = []
        parts.append("**MAVLink 遥测日志（pymavlink 解码）**\n\n")
        parts.append(f"- 文件大小: {size:,} bytes\n")
        parts.append(f"- 已解析消息数: {n_messages:,}"
                     + (f" *（达到上限 {_MAX_MAVLINK_MESSAGES:,}，未读完）*" if capped else "")
                     + "\n")
        if first_ts is not None and last_ts is not None:
            duration = last_ts - first_ts
            parts.append(f"- 起始时间戳: {first_ts:.3f}\n")
            parts.append(f"- 终止时间戳: {last_ts:.3f}\n")
            parts.append(f"- 时长: {duration:.2f} s\n")
        parts.append("\n")

        # 消息计数表
        if msg_counts:
            parts.append("### 消息类型统计\n\n")
            parts.append("| # | 消息类型 | 数量 |\n")
            parts.append("|---|----------|------|\n")
            sorted_counts = sorted(msg_counts.items(), key=lambda kv: -kv[1])
            for i, (mtype, cnt) in enumerate(sorted_counts, start=1):
                parts.append(f"| {i} | {mtype} | {cnt:,} |\n")
            parts.append("\n")

        # 样本消息
        if sample_head:
            parts.append(f"### 头部样本（前 {len(sample_head)} 条）\n\n")
            parts.append("```text\n")
            parts.extend(s + "\n" for s in sample_head)
            parts.append("```\n\n")
        if sample_tail:
            parts.append(f"### 尾部样本（最后 {len(sample_tail)} 条）\n\n")
            parts.append("```text\n")
            parts.extend(s + "\n" for s in sample_tail)
            parts.append("```\n")

        if capped:
            warnings.append(
                f"pymavlink 解析被限制在 {_MAX_MAVLINK_MESSAGES:,} 条消息，"
                "其余未读取（避免内存占用过大）"
            )

        # 应用日志体积上限：拼好的 markdown 字节数太大时同样要截断
        md = "".join(parts)
        md_bytes = md.encode("utf-8")
        if len(md_bytes) > self.cfg.log_file_max_bytes:
            cut, info = truncate_text_bytes(
                md_bytes,
                max_bytes=self.cfg.log_file_max_bytes,
                head_ratio=self.cfg.log_truncate_head_ratio,
            )
            md = cut.decode("utf-8", errors="replace")
            warnings.append(
                f"tlog 输出超过 LOG_FILE_MAX_BYTES，已对最终 markdown 做头+尾截断："
                f"原 {info.original_bytes:,} → 保留 {info.kept_bytes:,} 字节"
            )

        return ConversionResult(
            status="success",
            markdown=md,
            warnings=warnings,
            extra={
                "engine": "pymavlink",
                "original_bytes": size,
                "messages_parsed": n_messages,
                "messages_capped": capped,
                "message_types": len(msg_counts),
                "duration_seconds": (last_ts - first_ts) if (first_ts and last_ts) else None,
            },
        )

    @staticmethod
    def _format_message(m, t) -> str:
        """把单条 MAVLink 消息格式化成一行（仅取关键字段）。"""
        try:
            mtype = m.get_type()
            d = m.to_dict()
            # 去掉 mavpackettype（重复信息）
            d.pop("mavpackettype", None)
            kvs = ", ".join(f"{k}={v}" for k, v in d.items())
            ts_str = f"{t:.3f}" if t is not None else "?"
            return f"[{ts_str}] {mtype}: {kvs}"
        except Exception:
            return "<格式化失败>"

    # ---------- 元信息路径（pymavlink 缺席时的兜底）----------
    def _convert_metadata_only(
        self,
        path: Path,
        size: int,
        *,
        extra_warning: str | None = None,
    ) -> ConversionResult:
        try:
            head = path.read_bytes()[:256]
        except OSError as e:
            return ConversionResult(
                status="failed",
                markdown="",
                error=f"读取头部失败: {e.__class__.__name__}: {e}",
            )

        try:
            with path.open("rb") as f:
                f.seek(max(0, size - 256))
                tail = f.read()
        except OSError:
            tail = b""

        try:
            import hashlib
            with path.open("rb") as f:
                sha1 = hashlib.sha1()
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    sha1.update(chunk)
            sha1_hex = sha1.hexdigest()
        except Exception:
            sha1_hex = "(计算失败)"

        warnings: list[str] = [
            "未安装 pymavlink，无法解码 MAVLink 消息；"
            "仅输出文件元信息 + 头尾 hex 预览。如需解码：`pip install pymavlink`",
        ]
        if extra_warning:
            warnings.append(f"pymavlink 路径异常：{extra_warning}")

        parts = [
            "**MAVLink 遥测日志（仅元信息）**\n\n",
            f"- 文件大小: {size:,} bytes\n",
            f"- SHA-1: `{sha1_hex}`\n",
            f"- 解析方式: 元信息（未启用 pymavlink）\n\n",
            "### 头部 hex（前 256 bytes）\n\n",
            "```\n",
            _hex_preview(head),
            "```\n\n",
        ]
        if tail:
            parts += [
                "### 尾部 hex（最后 256 bytes）\n\n",
                "```\n",
                _hex_preview(tail),
                "```\n\n",
            ]
        parts.append(
            "> 💡 提示：tlog 是裸 MAVLink 字节流。要解码成可读消息，"
            "请执行 `pip install pymavlink` 后重跑本工具。\n"
        )

        return ConversionResult(
            status="success",
            markdown="".join(parts),
            warnings=warnings,
            extra={
                "engine": "metadata_only",
                "original_bytes": size,
                "sha1": sha1_hex,
            },
        )
