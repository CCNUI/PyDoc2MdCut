"""转换器基类。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from config import AppConfig


@dataclass
class ConversionResult:
    """单文件转换结果。

    - status: success / failed / skipped
    - markdown: 转换后的 markdown 正文（不含 FILE START/END 分隔符）。
                失败/跳过时也要给出占位文本。
    - warnings: 可选的警告列表（例如图片型 PDF、行数被截断等）
    - error: 失败原因摘要
    - skip_reason: 跳过原因（如 "OCR 未启用"、"不支持的扩展名"）
    - extra: 类型特定的附加字段（OCR 字符数、耗时、错误码等），用于报告
    """

    status: str  # "success" | "failed" | "skipped"
    markdown: str
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    skip_reason: str | None = None
    extra: dict = field(default_factory=dict)


class BaseConverter(ABC):
    """所有转换器的抽象基类。"""

    #: 子类要声明自己处理哪种 kind（与 scanner 中的 kind 一致）
    kind: str = ""

    def __init__(self, cfg: AppConfig) -> None:
        self.cfg = cfg

    @abstractmethod
    def convert(self, path: Path) -> ConversionResult:
        """把单个文件转成 markdown，返回 ConversionResult。

        子类不要抛异常出来——所有错误都包装成 status='failed' 的 ConversionResult。
        """
        ...

    # -------------------------------------------------------------
    # 通用截取助手：所有子类可在 convert() 末尾调用一次完成「头+尾+中间样本」
    # post-convert 层截取。pre-read 层在子类内部按需调用 _pre_read_truncate_bytes。
    # -------------------------------------------------------------

    def _pre_read_truncate_bytes(
        self, raw: bytes, *, path: Path
    ) -> tuple[bytes, "object", list[str]]:
        """对原始字节做 pre-read 截取（仅纯文本/日志类 converter 该调用）。

        Returns:
            (cut_bytes, TruncationInfo, warnings_added)
            warnings_added 是要追加到 ConversionResult.warnings 的字符串列表（可能为空）。
        """
        from truncation import truncate_bytes
        catalog = getattr(self.cfg, "truncation_catalog", None)
        if catalog is None or not getattr(self.cfg, "truncation_enabled", False):
            return raw, _empty_info(len(raw)), []
        spec = catalog.spec_for(ext=path.suffix.lower(), kind=self.kind)
        if not spec.is_meaningful():
            return raw, _empty_info(len(raw)), []
        cut, info = truncate_bytes(
            raw, spec,
            layer="pre-read",
            path_hint=str(path.name),
            global_seed=catalog.global_seed,
            marker_label="PRE-READ TRUNCATED",
        )
        warnings = [info.summary()] if info.truncated else []
        return cut, info, warnings

    def _post_convert_truncate(
        self, result: "ConversionResult", *, path: Path
    ) -> "ConversionResult":
        """对已生成的 markdown 做 post-convert 截取。

        - 仅在 status=='success' 且 catalog 启用时生效
        - 失败/跳过的占位 markdown 不截
        - 若已被 pre-read 截过，会被再算一层（通常这层不会再触发，因为产物已经够小）
        """
        from truncation import truncate_text
        catalog = getattr(self.cfg, "truncation_catalog", None)
        if catalog is None or not getattr(self.cfg, "truncation_enabled", False):
            return result
        if result.status != "success" or not result.markdown:
            return result
        spec = catalog.spec_for(ext=path.suffix.lower(), kind=self.kind)
        if not spec.is_meaningful():
            return result
        cut, info = truncate_text(
            result.markdown, spec,
            layer="post-convert",
            path_hint=str(path.name),
            global_seed=catalog.global_seed,
            marker_label="POST-CONVERT TRUNCATED",
        )
        if info.truncated:
            result.markdown = cut
            result.warnings.append(info.summary())
            # 把关键截取统计塞进 extra 供报告聚合
            result.extra.setdefault("post_truncation", {}).update({
                "truncated": True,
                "original_bytes": info.original_bytes,
                "kept_bytes": info.kept_bytes,
                "head_bytes": info.head_bytes,
                "tail_bytes": info.tail_bytes,
                "middle_bytes": info.middle_bytes,
                "middle_chunks_kept": info.middle_chunks_kept,
                "middle_chunks_requested": info.middle_chunks_requested,
                "skipped_bytes": info.skipped_bytes,
                "spec": info.spec_describe,
            })
            # 兼容旧字段（log/tlog/rlog 之前用的几个 extra key）
            result.extra.setdefault("truncated", True)
            result.extra.setdefault("original_bytes", info.original_bytes)
        return result


def _empty_info(n: int):
    """构造一个「未截断」的 TruncationInfo（避免循环导入）。"""
    from truncation import TruncationInfo
    return TruncationInfo(truncated=False, original_bytes=n, kept_bytes=n, head_bytes=n)
