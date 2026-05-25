"""通用文件截取模块。

提供「头部 xMB + 尾部 xMB + 中间随机抽取 N 个 xMB 块」的字节级截取算法，
并维护「每种文件类型独立配置」的查询接口。

设计要点：
- x 支持 0、小数、整数。x=0 即「该部分不取」。
- 随机抽样使用基于文件路径的种子，保证多次运行结果可复现，避免破坏查重 / 增量上传。
- 切分点尽量落在 UTF-8 字符边界与行边界（仅对文本字节有效；对纯二进制无效但不致命）。
- 配置默认值集中在 `truncation_defaults.py`，可被 `.env` 覆盖。

两层截取的语义：
- pre-read（仅文本类）：在 `path.read_bytes()` 后立即截取，避免后续 pandas/json
  解析阶段对超大文件做无意义的工作。截取产物会替代原始字节进入后续解析。
- post-convert（所有类型）：作用于转换器产出的 markdown 字节，最终落入合并 MD。
  对二进制类（pdf/docx/xlsx/wps/image/tlog/rlog 二进制段）只在这一层生效。

中间随机抽块算法：
- 把「头部边界 ~ 尾部边界」之间的中间区间 [m_lo, m_hi] 视为可抽样池。
- 期望抽取 N 块，每块大小 chunk_bytes。若中间区间 < N*chunk_bytes，则降到能放下的最大块数。
- 抽样起点之间至少保证不重叠；抽样起点尽量对齐到行边界（找不到就硬切到 UTF-8 边界）。
- 输出时按起点升序拼接，每块前后插入醒目的「... SAMPLE i/N ...」标记。
"""
from __future__ import annotations

import hashlib
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------- 数据结构 ----------

@dataclass
class TruncationSpec:
    """单类型截取的配置规格（来自 .env 或默认）。

    - enabled: 是否启用截取
    - head_mb / tail_mb / chunk_mb: 头部/尾部/中间单块大小，单位 MB（支持 0 / 小数 / 整数）
    - middle_chunks: 中间抽取多少个 chunk_mb 大小的块（0 = 不抽）
    - 当 head_mb + tail_mb + middle_chunks*chunk_mb >= 文件大小 时，等同于不截断
    - 三项全为 0 时实际不会保留任何内容；为了不输出空字符串，约定回退为「不截断」
    """
    enabled: bool = False
    head_mb: float = 0.0
    tail_mb: float = 0.0
    chunk_mb: float = 0.0
    middle_chunks: int = 0

    def is_meaningful(self) -> bool:
        """是否有实际作用：至少有一项 > 0 才有意义。"""
        if not self.enabled:
            return False
        if self.head_mb <= 0 and self.tail_mb <= 0 and (
            self.middle_chunks <= 0 or self.chunk_mb <= 0
        ):
            return False
        return True

    def head_bytes_target(self) -> int:
        return int(self.head_mb * 1024 * 1024)

    def tail_bytes_target(self) -> int:
        return int(self.tail_mb * 1024 * 1024)

    def chunk_bytes_target(self) -> int:
        return int(self.chunk_mb * 1024 * 1024)

    def describe(self) -> str:
        if not self.enabled:
            return "禁用"
        parts = []
        if self.head_mb > 0:
            parts.append(f"头 {self.head_mb}MB")
        if self.tail_mb > 0:
            parts.append(f"尾 {self.tail_mb}MB")
        if self.middle_chunks > 0 and self.chunk_mb > 0:
            parts.append(f"中间随机 {self.middle_chunks}×{self.chunk_mb}MB")
        if not parts:
            return "启用但全为 0（实际不截取）"
        return " + ".join(parts)


@dataclass
class TruncationInfo:
    """截取产物的元信息，用于报告 / extra 字段。"""
    truncated: bool = False
    layer: str = ""               # 'pre-read' / 'post-convert' / ''
    original_bytes: int = 0
    kept_bytes: int = 0
    head_bytes: int = 0
    tail_bytes: int = 0
    middle_bytes: int = 0
    middle_chunks_kept: int = 0
    middle_chunks_requested: int = 0
    skipped_bytes: int = 0
    # 每个被采样块的 [start, end) 偏移（用于报告人审）
    sample_offsets: list[tuple[int, int]] = field(default_factory=list)
    spec_describe: str = ""

    def summary(self) -> str:
        if not self.truncated:
            return f"未截断（原始 {self.original_bytes:,} 字节）"
        s = (
            f"已截断[{self.layer}]: 原始 {self.original_bytes:,} → 保留 "
            f"{self.kept_bytes:,} 字节"
        )
        bits = []
        if self.head_bytes:
            bits.append(f"头 {self.head_bytes:,}")
        if self.tail_bytes:
            bits.append(f"尾 {self.tail_bytes:,}")
        if self.middle_bytes:
            bits.append(
                f"中间 {self.middle_chunks_kept}/{self.middle_chunks_requested}"
                f"×块 共 {self.middle_bytes:,}"
            )
        if bits:
            s += "（" + " + ".join(bits) + f"，舍弃 {self.skipped_bytes:,}）"
        return s


# ---------- UTF-8 / 行边界对齐工具 ----------

def _safe_utf8_back(raw: bytes, idx: int) -> int:
    """向左收缩到 UTF-8 字符边界。"""
    i = min(idx, len(raw))
    while i > 0 and (raw[i] & 0xC0) == 0x80:
        i -= 1
    return i


def _safe_utf8_forward(raw: bytes, idx: int) -> int:
    """向右扩展到 UTF-8 字符边界。"""
    i = max(idx, 0)
    while i < len(raw) and (raw[i] & 0xC0) == 0x80:
        i += 1
    return i


def _align_head_end(raw: bytes, target: int) -> int:
    """头部结束位置：优先靠前找最后一个换行；找不到再 UTF-8 边界硬切。"""
    if target >= len(raw):
        return len(raw)
    if target <= 0:
        return 0
    nl = raw.rfind(b"\n", 0, target)
    if nl != -1 and nl >= target // 2:
        return nl + 1
    return _safe_utf8_back(raw, target)


def _align_tail_start(raw: bytes, target_start: int) -> int:
    """尾部起始位置：优先找下一个换行后；否则 UTF-8 边界。"""
    if target_start <= 0:
        return 0
    if target_start >= len(raw):
        return len(raw)
    nl = raw.find(b"\n", target_start, len(raw))
    if nl != -1 and nl - target_start < 64 * 1024:  # 行很长时不无限找
        return nl + 1
    return _safe_utf8_forward(raw, target_start)


def _align_chunk(raw: bytes, start: int, length: int) -> tuple[int, int]:
    """对中间抽样的单个 chunk 做边界对齐。"""
    if length <= 0:
        return start, start
    end = min(start + length, len(raw))
    # 起点：尽量向后挪到换行后（避免半行开头）；但不要挪太多以免把块挤没
    nl_s = raw.find(b"\n", start, min(start + 4096, end))
    if nl_s != -1 and nl_s + 1 < end:
        start = nl_s + 1
    else:
        start = _safe_utf8_forward(raw, start)
    # 终点：尽量向前挪到换行处
    nl_e = raw.rfind(b"\n", start, end)
    if nl_e != -1 and nl_e - start > length // 2:
        end = nl_e + 1
    else:
        end = _safe_utf8_back(raw, end)
    if end < start:
        end = start
    return start, end


# ---------- 种子 ----------

def _seed_for(path_hint: str, global_seed: int) -> int:
    """根据文件路径与全局种子生成稳定 random 种子。

    - path_hint 通常传相对路径字符串
    - 同一个文件 + 同一个全局种子 ⇒ 同一序列 ⇒ 可复现
    """
    h = hashlib.sha1(f"{global_seed}:{path_hint}".encode("utf-8")).digest()
    # 取前 8 字节做整型种子
    return int.from_bytes(h[:8], "big", signed=False)


# ---------- 中间抽样核心 ----------

def _pick_middle_chunk_starts(
    middle_lo: int,
    middle_hi: int,
    chunk_size: int,
    n_chunks: int,
    rng: random.Random,
) -> list[int]:
    """在 [middle_lo, middle_hi) 区间内选 n_chunks 个不重叠的 chunk_size 块起点。

    实现：把可用空间按 chunk_size 等分成「槽位」，再从槽位里无放回抽样。
    这样保证块之间不重叠。
    """
    if chunk_size <= 0 or n_chunks <= 0:
        return []
    available = middle_hi - middle_lo
    if available <= 0:
        return []
    n_slots = available // chunk_size
    if n_slots <= 0:
        return []
    real_n = min(n_chunks, n_slots)
    slot_indices = rng.sample(range(n_slots), real_n)
    slot_indices.sort()
    return [middle_lo + s * chunk_size for s in slot_indices]


# ---------- 公共截取函数 ----------

def truncate_bytes(
    raw: bytes,
    spec: TruncationSpec,
    *,
    layer: str,
    path_hint: str,
    global_seed: int,
    marker_label: str = "TRUNCATED",
) -> tuple[bytes, TruncationInfo]:
    """按 TruncationSpec 对 raw 做「头 + 尾 + 中间随机抽块」截取。

    Args:
        raw: 原始字节流
        spec: 该类型对应的截取规格
        layer: 哪一层调用，仅用于报告标记（'pre-read' / 'post-convert'）
        path_hint: 用于生成稳定随机种子的字符串（推荐：相对路径）
        global_seed: 全局随机种子（来自 .env 或默认 0）
        marker_label: 中间标记里显示的层标签
    """
    n = len(raw)
    info = TruncationInfo(
        original_bytes=n,
        kept_bytes=n,
        head_bytes=n,
        layer=layer,
        spec_describe=spec.describe(),
        middle_chunks_requested=spec.middle_chunks if spec.enabled else 0,
    )

    if n == 0 or not spec.is_meaningful():
        return raw, info

    head_t = max(0, spec.head_bytes_target())
    tail_t = max(0, spec.tail_bytes_target())
    chunk_t = max(0, spec.chunk_bytes_target())
    n_chunks = max(0, spec.middle_chunks) if chunk_t > 0 else 0

    total_target = head_t + tail_t + n_chunks * chunk_t
    if total_target >= n:
        # 不需要截断
        return raw, info

    # 1) 头部边界
    head_end = _align_head_end(raw, head_t)

    # 2) 尾部边界
    if tail_t > 0:
        tail_start_raw = max(head_end, n - tail_t)
        tail_start = _align_tail_start(raw, tail_start_raw)
    else:
        tail_start = n  # 不取尾部

    if tail_start < head_end:
        tail_start = head_end  # 防御

    # 3) 中间抽样：从 [head_end, tail_start) 区间挑 n_chunks 个 chunk_t 大小的块
    rng = random.Random(_seed_for(path_hint, global_seed))
    sample_starts = _pick_middle_chunk_starts(
        head_end, tail_start, chunk_t, n_chunks, rng
    )

    # 4) 对每个采样块做边界对齐
    aligned_samples: list[tuple[int, int]] = []
    for s in sample_starts:
        a_s, a_e = _align_chunk(raw, s, chunk_t)
        if a_e > a_s:
            aligned_samples.append((a_s, a_e))
    aligned_samples.sort(key=lambda p: p[0])

    # 去重叠（保险）
    deduped: list[tuple[int, int]] = []
    last_end = head_end
    for s, e in aligned_samples:
        if s < last_end:
            s = last_end
        if e <= s:
            continue
        if s >= tail_start:
            break
        if e > tail_start:
            e = tail_start
        deduped.append((s, e))
        last_end = e
    aligned_samples = deduped

    # 5) 拼装
    parts: list[bytes] = []
    head_part = raw[:head_end]
    parts.append(head_part)

    middle_total = 0
    prev_end = head_end
    for idx, (s, e) in enumerate(aligned_samples, start=1):
        gap = s - prev_end
        if gap > 0:
            marker = (
                f"\n\n... {marker_label} (skip {gap:,} bytes) ...\n\n"
            ).encode("utf-8")
            parts.append(marker)
        sample_marker = (
            f"\n\n--- SAMPLE {idx}/{len(aligned_samples)} "
            f"[offset {s:,}-{e:,}, {e - s:,} bytes] ---\n\n"
        ).encode("utf-8")
        parts.append(sample_marker)
        parts.append(raw[s:e])
        middle_total += e - s
        prev_end = e

    # 中间到尾部之间的跳过标记
    gap_before_tail = tail_start - prev_end
    if gap_before_tail > 0 and tail_start < n:
        marker = (
            f"\n\n... {marker_label} (skip {gap_before_tail:,} bytes) ...\n\n"
        ).encode("utf-8")
        parts.append(marker)

    tail_part = raw[tail_start:] if tail_start < n else b""
    if tail_part:
        # 在尾部前加一个 TAIL 标签让 LLM 容易辨认
        tail_marker = (
            f"\n\n--- TAIL [offset {tail_start:,}-{n:,}, "
            f"{len(tail_part):,} bytes] ---\n\n"
        ).encode("utf-8")
        parts.append(tail_marker)
        parts.append(tail_part)

    out = b"".join(parts)

    skipped = n - (len(head_part) + middle_total + len(tail_part))
    info = TruncationInfo(
        truncated=True,
        layer=layer,
        original_bytes=n,
        kept_bytes=len(head_part) + middle_total + len(tail_part),
        head_bytes=len(head_part),
        tail_bytes=len(tail_part),
        middle_bytes=middle_total,
        middle_chunks_kept=len(aligned_samples),
        middle_chunks_requested=n_chunks,
        skipped_bytes=max(0, skipped),
        sample_offsets=aligned_samples,
        spec_describe=spec.describe(),
    )
    return out, info


def truncate_text(
    text: str,
    spec: TruncationSpec,
    *,
    layer: str,
    path_hint: str,
    global_seed: int,
    marker_label: str = "TRUNCATED",
) -> tuple[str, TruncationInfo]:
    """字符串版本：内部走 UTF-8 字节级截取，再回解为字符串。"""
    raw = text.encode("utf-8")
    cut, info = truncate_bytes(
        raw, spec, layer=layer, path_hint=path_hint,
        global_seed=global_seed, marker_label=marker_label,
    )
    if not info.truncated:
        return text, info
    # 解码后插入的标记本身已是 UTF-8，errors='replace' 仅防御性兜底
    return cut.decode("utf-8", errors="replace"), info


# ---------- 截取规格目录 (TruncationCatalog) ----------

# 同一类型的规格按以下顺序查找：扩展名（含点小写） > kind > "default"
# 不存在则返回禁用的占位规格。

@dataclass
class TruncationCatalog:
    """统一管理所有类型的 TruncationSpec。

    使用方式：
        spec = catalog.spec_for(ext='.log', kind='log')
    """
    by_ext: dict[str, TruncationSpec] = field(default_factory=dict)
    by_kind: dict[str, TruncationSpec] = field(default_factory=dict)
    default_spec: TruncationSpec = field(default_factory=TruncationSpec)
    global_seed: int = 0

    def spec_for(self, *, ext: str, kind: str) -> TruncationSpec:
        ext_key = (ext or "").lower()
        if ext_key in self.by_ext:
            return self.by_ext[ext_key]
        if kind in self.by_kind:
            return self.by_kind[kind]
        return self.default_spec

    def known_keys_describe(self) -> list[str]:
        """生成一份可打印的「键 → 规格」列表（用于启动日志）。"""
        out: list[str] = []
        for ext, sp in sorted(self.by_ext.items()):
            out.append(f"  ext  {ext:<10} → {sp.describe()}")
        for kind, sp in sorted(self.by_kind.items()):
            out.append(f"  kind {kind:<10} → {sp.describe()}")
        out.append(f"  default            → {self.default_spec.describe()}")
        return out
