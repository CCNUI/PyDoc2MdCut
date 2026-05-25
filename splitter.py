"""分卷模块。

策略：
1. header（标题 + TOC）总是放在 第 1 卷开头。
2. 优先在 FILE END 之后切分。
3. 单个文件块本身超过卷限额时，必须中间切：
   - 切点前插入 PART BREAK 标记
   - 下一卷续接前插入 CONTINUED 标记
   - 中间切只在文本边界（行边界）做，避免破坏 UTF-8 字节
4. 每卷有卷头（VOLUME N/M）和卷尾（END OF VOLUME N/M）。
5. 最后一卷追加 END OF BUNDLE。

实现思路：
- 先把所有内容（header + 各 file_block）按"块"序列收集；每个块知道自己属于哪个 file_id 或是 header。
- 第一遍：按 max_size_bytes 做切分规划，得到每卷的内容列表。需要做大文件中间切的，把文件块拆成多段，每段标 part_index/total_parts。
- 第二遍：知道总卷数后，给每卷加上正确的卷头/卷尾。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from config import AppConfig
from dedup import DupReport
from merger import FileEntry, build_header

logger = logging.getLogger(__name__)


# 用 UTF-8 字节算大小
def _utf8_len(s: str) -> int:
    return len(s.encode("utf-8"))


# ============================================================================
# Piece 数据源类型
# ============================================================================
# 一个 piece 可能来自两种数据源:
#   - "inline"       : 直接持有一段 text (用于 header / 占位 / 标记字符串等小段)
#   - "spool_slice"  : 引用 spool 文件的 [byte_start, byte_end) 区间
#                      (用于文件 block,可能是完整文件,也可能是中间切的一段)
# ============================================================================


@dataclass
class _PieceInfo:
    """卷规划的一段内容(不持有大文本)。

    - source_kind: "inline" 或 "spool_slice"
    - inline_text: 仅 source_kind=="inline" 时使用,小段文本(header / 标记)
    - spool_path / byte_start / byte_end: 仅 source_kind=="spool_slice" 时使用,
       指向 spool 文件的字节区间;写卷时按区间从磁盘读出来直接写到卷文件句柄。
    - bytes_len: 这段 piece 的 UTF-8 字节大小(用于卷规划)
    - file_id / rel_path / part_index / total_parts / is_first_part / is_last_part:
       辅助信息,与原版兼容。
    """
    source_kind: str  # "inline" | "spool_slice"
    bytes_len: int
    inline_text: str = ""
    spool_path: Path | None = None
    byte_start: int = 0
    byte_end: int = 0
    file_id: str | None = None
    rel_path: str | None = None
    part_index: int | None = None
    total_parts: int | None = None
    is_last_part: bool = False
    is_first_part: bool = False

    @classmethod
    def inline(cls, text: str) -> "_PieceInfo":
        return cls(
            source_kind="inline",
            inline_text=text,
            bytes_len=_utf8_len(text),
        )

    @classmethod
    def spool(
        cls,
        spool_path: Path,
        byte_start: int,
        byte_end: int,
        *,
        file_id: str | None = None,
        rel_path: str | None = None,
        part_index: int | None = None,
        total_parts: int | None = None,
        is_first_part: bool = False,
        is_last_part: bool = False,
    ) -> "_PieceInfo":
        return cls(
            source_kind="spool_slice",
            bytes_len=byte_end - byte_start,
            spool_path=spool_path,
            byte_start=byte_start,
            byte_end=byte_end,
            file_id=file_id,
            rel_path=rel_path,
            part_index=part_index,
            total_parts=total_parts,
            is_first_part=is_first_part,
            is_last_part=is_last_part,
        )

    def read_bytes(self) -> bytes:
        """把 piece 的内容读成 bytes,用于写卷。"""
        if self.source_kind == "inline":
            return self.inline_text.encode("utf-8")
        if self.spool_path is None:
            return b""
        with self.spool_path.open("rb") as f:
            f.seek(self.byte_start)
            return f.read(self.byte_end - self.byte_start)


@dataclass
class _VolumePlan:
    pieces: list[_PieceInfo] = field(default_factory=list)
    bytes_used: int = 0
    # 完整文件计数（作为 piece 一次性写入的）
    complete_files: int = 0
    # 续接信息
    continues_from_prev: tuple[str, str, int, int] | None = None  # (rel_path, file_id, part_idx, total)
    continues_to_next: tuple[str, str, int, int] | None = None


def split_and_write(
    cfg: AppConfig,
    entries: list[FileEntry],
    *,
    dup_report: DupReport | None = None,
    all_files: "list | None" = None,
) -> list[Path]:
    """规划分卷、写入文件,返回输出文件路径列表(按卷顺序)。

    内存友好实现:
      - 不再 `[build_file_block(e) for e in entries]` 一次性构造所有 block
      - 每个文件的 block 已经由主流程写在 cfg.spool_dir 下
      - 规划阶段只看 spool 文件大小(已记在 result.block_spool_bytes)
      - 渲染阶段从 spool 流式读字节,直接 stream 写到卷文件句柄

    - all_files: 全量枚举结果(包括被进目录筛选拒绝的)。如启用,
      会在 header 中插入「完整文件目录」章节。
    """
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    # 1) 构造 header(永远放第 1 卷开头)
    header_text = build_header(cfg, entries, dup_report=dup_report, all_files=all_files)

    # 2) 规划分卷(流式:不构造全量 file_blocks 字符串列表)
    volumes = _plan_volumes_streaming(
        cfg=cfg,
        header_text=header_text,
        entries=entries,
    )
    total_volumes = len(volumes)

    # 3) 写文件(流式:逐 piece 读 spool,直接写卷句柄)
    out_paths: list[Path] = []
    for idx, vol in enumerate(volumes, start=1):
        filename = f"merged_part_{idx:03d}_of_{total_volumes:03d}.md"
        path = cfg.output_dir / filename
        written_bytes = _write_volume_streaming(
            cfg=cfg,
            vol=vol,
            volume_index=idx,
            total_volumes=total_volumes,
            path=path,
        )
        out_paths.append(path)
        logger.info(
            f"写入 {filename}: {written_bytes} bytes / "
            f"{cfg.max_size_bytes} 限额"
        )

    # 4) 清理 spool 目录(主流程产物,不再需要)
    _cleanup_spool(cfg)

    return out_paths


def _cleanup_spool(cfg: AppConfig) -> None:
    """清理 spool 目录。失败不致命。"""
    import shutil
    spool = cfg.spool_dir
    if not spool.exists():
        return
    try:
        shutil.rmtree(spool)
        logger.info(f"已清理 spool 目录: {spool}")
    except Exception as e:
        logger.warning(
            f"清理 spool 目录失败(不影响输出): {spool} - "
            f"{e.__class__.__name__}: {e}"
        )


def _plan_volumes_streaming(
    *,
    cfg: AppConfig,
    header_text: str,
    entries: list[FileEntry],
) -> list[_VolumePlan]:
    """规划分卷内容(不渲染卷头卷尾,不持有大文本)。

    每个 entry 的 file_block 已经写在 cfg.spool_dir/<file_id>.block。
    规划时只看 result.block_spool_bytes(已记录);
    需要中间切大文件时才打开 spool 文件按字节找换行切分。
    """
    HEAD_TAIL_RESERVE = 1024

    effective_max = cfg.max_size_bytes - HEAD_TAIL_RESERVE
    if effective_max <= 0:
        raise SystemExit(
            f"[分卷错误] max_size_mb={cfg.max_size_mb} 太小,"
            f"扣除卷头卷尾预留 {HEAD_TAIL_RESERVE} 字节后已为非正数。"
        )

    volumes: list[_VolumePlan] = [_VolumePlan()]
    current = volumes[0]

    # header 作为 inline piece
    header_bytes = _utf8_len(header_text)
    if header_bytes >= effective_max:
        logger.warning(
            f"文件头大小 {header_bytes} 已接近/超过单卷限额 {effective_max},"
            "将独占第 1 卷。"
        )
    current.pieces.append(_PieceInfo.inline(header_text))
    current.bytes_used = header_bytes

    for entry in entries:
        spool_path = entry.result.block_spool_path
        block_bytes = entry.result.block_spool_bytes
        rel_path = str(entry.scanned.rel_path)
        fid = entry.scanned.file_id

        if spool_path is None or block_bytes == 0:
            # 回落: spool 写入失败的极少数情况
            fallback_text = (
                f"\n\n<!-- [SPOOL MISSING] file_id={fid} rel_path={rel_path} -->\n"
                f"> WARN 此文件转换结果未能写入 spool,内容不可恢复。\n\n"
            )
            fb_bytes = _utf8_len(fallback_text)
            if current.bytes_used + fb_bytes > effective_max:
                current = _VolumePlan()
                volumes.append(current)
            current.pieces.append(_PieceInfo.inline(fallback_text))
            current.bytes_used += fb_bytes
            current.complete_files += 1
            continue

        # 整块塞入当前卷
        if current.bytes_used + block_bytes <= effective_max:
            current.pieces.append(_PieceInfo.spool(
                spool_path, 0, block_bytes,
                file_id=fid, rel_path=rel_path,
                is_first_part=True, is_last_part=True,
            ))
            current.bytes_used += block_bytes
            current.complete_files += 1
            continue

        # 起新卷整块塞
        if block_bytes <= effective_max:
            current = _VolumePlan()
            volumes.append(current)
            current.pieces.append(_PieceInfo.spool(
                spool_path, 0, block_bytes,
                file_id=fid, rel_path=rel_path,
                is_first_part=True, is_last_part=True,
            ))
            current.bytes_used = block_bytes
            current.complete_files += 1
            continue

        # 单文件超卷, 中间切
        cut_points = _plan_spool_cuts(
            spool_path=spool_path,
            total_bytes=block_bytes,
            current_remaining=effective_max - current.bytes_used,
            volume_capacity=effective_max,
        )
        total_parts = len(cut_points)
        for i, (b0, b1) in enumerate(cut_points, start=1):
            piece = _PieceInfo.spool(
                spool_path, b0, b1,
                file_id=fid, rel_path=rel_path,
                part_index=i, total_parts=total_parts,
                is_first_part=(i == 1),
                is_last_part=(i == total_parts),
            )
            if i == 1:
                current.pieces.append(piece)
                current.bytes_used += piece.bytes_len
                current.continues_to_next = (rel_path, fid, i, total_parts)
            else:
                current = _VolumePlan()
                volumes.append(current)
                current.pieces.append(piece)
                current.bytes_used = piece.bytes_len
                current.continues_from_prev = (rel_path, fid, i, total_parts)
                if i < total_parts:
                    current.continues_to_next = (rel_path, fid, i, total_parts)

    return volumes


def _plan_spool_cuts(
    *,
    spool_path: Path,
    total_bytes: int,
    current_remaining: int,
    volume_capacity: int,
) -> list[tuple[int, int]]:
    """规划单个 spool 文件的中间切点,返回 [(byte_start, byte_end), ...]。
    切点对齐行边界(\\n),不切多字节字符中间。
    实现上每次只读切点附近的窗口(<= 卷大小),不加载整个 block 到内存。
    """
    MARKER_RESERVE = 256
    cuts: list[tuple[int, int]] = []
    pos = 0
    first = True

    with spool_path.open("rb") as f:
        while pos < total_bytes:
            if first:
                limit = current_remaining - MARKER_RESERVE
                first = False
            else:
                limit = volume_capacity - MARKER_RESERVE * 2

            if limit < 1024:
                limit = volume_capacity - MARKER_RESERVE * 2

            end = pos + limit
            if end >= total_bytes:
                cuts.append((pos, total_bytes))
                break

            f.seek(pos)
            window = f.read(end - pos)  # <= 卷限额, OK
            cut_rel = window.rfind(b"\n")
            if cut_rel == -1:
                cut = _safe_utf8_cut_in_file(f, end)
                if cut <= pos:
                    cut = end
            else:
                cut = pos + cut_rel + 1
            cuts.append((pos, cut))
            pos = cut

    return cuts


def _safe_utf8_cut_in_file(f, idx: int) -> int:
    """在文件中找一个不切到 UTF-8 多字节字符中间的位置(最多回退 4 字节)。"""
    start = max(0, idx - 8)
    f.seek(start)
    buf = f.read(idx - start)
    i = len(buf)
    while i > 0 and (buf[i - 1] & 0xC0) == 0x80:
        i -= 1
    if i == 0:
        return idx
    return start + i


# ============================================================================
# 流式写卷
# ============================================================================

def _write_volume_streaming(
    *,
    cfg: AppConfig,
    vol: _VolumePlan,
    volume_index: int,
    total_volumes: int,
    path: Path,
) -> int:
    """流式写一卷, 不在内存里拼成大字符串。

    返回:实际写入的字节数。
    """
    # 卷头(小,直接 build string)
    head_text = _build_volume_head(cfg, vol, volume_index, total_volumes)

    # 卷尾
    tail_text = _build_volume_tail(volume_index, total_volumes)

    total_written = 0
    with path.open("wb") as out:
        # head
        head_bytes = head_text.encode("utf-8")
        out.write(head_bytes)
        total_written += len(head_bytes)

        # continued marker
        if vol.continues_from_prev:
            _, fid, _, _ = vol.continues_from_prev
            marker = f"\n===== CONTINUED FILE ID={fid} FROM PREVIOUS VOLUME =====\n\n"
            mb = marker.encode("utf-8")
            out.write(mb)
            total_written += len(mb)

        # pieces -- 逐 piece 读 spool 范围, 直接写出
        BUF_SIZE = 64 * 1024  # 64KB 缓冲
        for piece in vol.pieces:
            if piece.source_kind == "inline":
                inline_bytes = piece.inline_text.encode("utf-8")
                out.write(inline_bytes)
                total_written += len(inline_bytes)
            elif piece.source_kind == "spool_slice" and piece.spool_path is not None:
                remaining = piece.byte_end - piece.byte_start
                with piece.spool_path.open("rb") as sp:
                    sp.seek(piece.byte_start)
                    while remaining > 0:
                        chunk = sp.read(min(BUF_SIZE, remaining))
                        if not chunk:
                            break
                        out.write(chunk)
                        total_written += len(chunk)
                        remaining -= len(chunk)

        # part-break marker
        if vol.continues_to_next:
            _, fid, _, _ = vol.continues_to_next
            marker = f"\n===== PART BREAK FILE ID={fid} TO BE CONTINUED IN NEXT VOLUME =====\n\n"
            mb = marker.encode("utf-8")
            out.write(mb)
            total_written += len(mb)

        # tail
        tail_bytes = tail_text.encode("utf-8")
        out.write(tail_bytes)
        total_written += len(tail_bytes)

    return total_written


def _build_volume_head(
    cfg: AppConfig, vol: _VolumePlan, volume_index: int, total_volumes: int
) -> str:
    """卷头(小文本)。"""
    head_lines: list[str] = []
    head_lines.append("<!-- ================================================================ -->")
    head_lines.append(f"<!-- ===============  VOLUME {volume_index} / {total_volumes}  =============== -->")
    head_lines.append("<!-- ================================================================ -->")
    head_lines.append("")
    head_lines.append(f"# Volume {volume_index} of {total_volumes}")
    head_lines.append("")
    bytes_used = sum(p.bytes_len for p in vol.pieces)
    head_lines.append(f"- 卷文件名: merged_part_{volume_index:03d}_of_{total_volumes:03d}.md")
    head_lines.append(f"- 字节数: {bytes_used:,} / {cfg.max_size_bytes:,} (限额)")
    head_lines.append(f"- 本卷包含完整文件: {vol.complete_files} 个")
    if vol.continues_from_prev:
        rel, fid, pi, tp = vol.continues_from_prev
        head_lines.append(f"- 续接上一卷的文件: {rel} (ID={fid}) -- 第 {pi}/{tp} 段")
    else:
        head_lines.append("- 续接上一卷的文件: 无")
    if vol.continues_to_next:
        rel, fid, pi, tp = vol.continues_to_next
        head_lines.append(f"- 将续接到下一卷的文件: {rel} (ID={fid}) -- 当前第 {pi}/{tp} 段")
    else:
        head_lines.append("- 将续接到下一卷的文件: 无")
    if volume_index > 1:
        head_lines.append(f"- 上一卷: merged_part_{volume_index - 1:03d}_of_{total_volumes:03d}.md")
    else:
        head_lines.append("- 上一卷: 无(本卷为首卷)")
    if volume_index < total_volumes:
        head_lines.append(f"- 下一卷: merged_part_{volume_index + 1:03d}_of_{total_volumes:03d}.md")
    else:
        head_lines.append("- 下一卷: 无(本卷为末卷)")
    head_lines.append("")
    head_lines.append("---")
    head_lines.append("")
    return "\n".join(head_lines) + "\n"


def _build_volume_tail(volume_index: int, total_volumes: int) -> str:
    """卷尾(小文本)。"""
    tail_lines: list[str] = []
    tail_lines.append("")
    tail_lines.append("---")
    tail_lines.append("")
    tail_lines.append("<!-- ================================================================ -->")
    tail_lines.append(f"<!-- ===============  END OF VOLUME {volume_index} / {total_volumes}  =============== -->")
    tail_lines.append("<!-- ================================================================ -->")
    tail_lines.append("")
    tail_lines.append(f"# End of Volume {volume_index} / {total_volumes}")
    if volume_index < total_volumes:
        tail_lines.append(f"- 下一卷: merged_part_{volume_index + 1:03d}_of_{total_volumes:03d}.md")
    else:
        tail_lines.append('- 下一卷: 无(本卷为末卷,下方为 END OF BUNDLE)')
    if volume_index == total_volumes:
        tail_lines.append("")
        tail_lines.append("# END OF BUNDLE")
        tail_lines.append(f"- 全部 {total_volumes} 卷结束。详细统计见 conversion_report.md。")
    return "\n".join(tail_lines) + "\n"
