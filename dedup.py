"""重复文件检测与 AI 友好报告渲染。

调用前置条件：所有 ScannedFile 都已经通过 hasher 填充了 content_hash。
（没有 hash 的文件 —— 比如被 FILE_HASH_MAX_BYTES 跳过的 —— 不参与查重，
但会列在报告底部的"未参与查重"清单里。）
"""
from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

from config import AppConfig
from scanner import ScannedFile

logger = logging.getLogger(__name__)


_ALGO_LABEL = {
    "sha256": "SHA-256",
    "sha1":   "SHA-1",
    "md5":    "MD5",
}


@dataclass
class DupGroup:
    """重复文件组：内容哈希相同的一组文件。"""
    content_hash: str
    size_bytes: int
    files: list[ScannedFile]   # 至少 2 个；首个为"主副本"

    @property
    def main(self) -> ScannedFile:
        return self.files[0]

    @property
    def duplicates(self) -> list[ScannedFile]:
        return self.files[1:]

    @property
    def redundant_bytes(self) -> int:
        """冗余字节数（除去主副本以外的文件占用）。"""
        return self.size_bytes * len(self.duplicates)


@dataclass
class DupReport:
    groups: list[DupGroup]
    unhashed: list[ScannedFile]    # 没拿到 hash 的文件（被跳过 / 失败）
    algo: str

    @property
    def total_redundant_files(self) -> int:
        return sum(len(g.duplicates) for g in self.groups)

    @property
    def total_files_in_dups(self) -> int:
        return sum(len(g.files) for g in self.groups)

    @property
    def total_redundant_bytes(self) -> int:
        return sum(g.redundant_bytes for g in self.groups)


def find_duplicates(cfg: AppConfig, files: list[ScannedFile]) -> DupReport:
    """根据 content_hash 把 files 分组。返回 DupReport。"""
    by_hash: dict[str, list[ScannedFile]] = defaultdict(list)
    unhashed: list[ScannedFile] = []
    for sf in files:
        if sf.content_hash:
            by_hash[sf.content_hash].append(sf)
        else:
            unhashed.append(sf)

    groups: list[DupGroup] = []
    for h, group_files in by_hash.items():
        if len(group_files) < 2:
            continue
        # 主副本选取：相对路径字典序最靠前的（稳定可复现）
        sorted_files = sorted(group_files, key=lambda f: str(f.rel_path).lower())
        groups.append(
            DupGroup(
                content_hash=h,
                size_bytes=sorted_files[0].size_bytes,
                files=sorted_files,
            )
        )
    # 组之间按"冗余字节数"降序排（最大水分先看到），再按主副本路径稳定
    groups.sort(
        key=lambda g: (-g.redundant_bytes, str(g.main.rel_path).lower())
    )

    logger.info(
        f"查重完成：发现 {len(groups)} 个重复组，"
        f"涉及 {sum(len(g.files) for g in groups)} 个文件，"
        f"冗余 {sum(g.redundant_bytes for g in groups):,} bytes"
    )

    return DupReport(groups=groups, unhashed=unhashed, algo=cfg.file_hash_algo)


# ----------------------------------------------------------------------------
# 渲染部分：生成给 LLM 看的 markdown
# ----------------------------------------------------------------------------

def render_dup_report_markdown(rep: DupReport) -> str:
    """生成 AI 友好的查重报告 markdown，准备插在 TOC 之后。

    设计目标：
    1. 顶部明确告诉 LLM 这是什么、怎么用
    2. 每组结构化（哈希 / 大小 / 主副本 / 副本列表）
    3. 提供 file_id 让 LLM 可以反查每个文件的正文位置
    """
    algo_label = _ALGO_LABEL.get(rep.algo, rep.algo)

    lines: list[str] = []
    lines.append("## 🔁 重复文件报告 (Duplicate Files Report)\n")
    lines.append("")
    lines.append("> **AI 阅读说明 (AI Reading Hint)**")
    lines.append("> ")
    lines.append(
        "> 下面列出的是**内容完全相同**（相同 "
        f"{algo_label} 哈希）的重复文件组。"
    )
    lines.append("> 处理建议：")
    lines.append("> - 在跨文件分析、检索、问答时，**每个组内的文件可视为同一份内容的多个副本**。")
    lines.append("> - 引用时，优先引用每组的"
                 "**主副本** (标记为 `[MAIN]`)，避免在回答中重复同一份内容。")
    lines.append("> - 主副本选取规则：组内**相对路径字典序最靠前**的文件。")
    lines.append("> - 每个文件的 `file_id` 与正文 `===== FILE ID: ... =====` 段落一一对应。")
    lines.append("")
    lines.append("**统计：**")
    lines.append(f"- 哈希算法: {algo_label}")
    lines.append(f"- 重复组数: {len(rep.groups)}")
    lines.append(
        f"- 涉及文件数: {rep.total_files_in_dups} "
        f"（其中冗余副本 {rep.total_redundant_files}）"
    )
    lines.append(f"- 冗余字节数: {rep.total_redundant_bytes:,} "
                 f"({_human_bytes(rep.total_redundant_bytes)})")
    if rep.unhashed:
        lines.append(
            f"- 未参与查重: {len(rep.unhashed)} 个文件（哈希被跳过或失败，详见末尾）"
        )
    lines.append("")

    if not rep.groups:
        lines.append("✅ **无重复文件**")
        lines.append("")
    else:
        # 紧凑机器友好块（方便 LLM 一次性消化所有组的文件 ID 关系）
        lines.append("**机器可读索引 (machine-readable index):**")
        lines.append("")
        lines.append("```")
        lines.append("# format: <group_idx>\t<role>\t<file_id>\t<size>\t<rel_path>")
        for gi, g in enumerate(rep.groups, start=1):
            for fi, sf in enumerate(g.files):
                role = "MAIN" if fi == 0 else "DUP"
                lines.append(
                    f"{gi}\t{role}\t{sf.file_id}\t{sf.size_bytes}\t{sf.rel_path}"
                )
        lines.append("```")
        lines.append("")

        # 然后给人类可读的分组详情
        for gi, g in enumerate(rep.groups, start=1):
            short_h = g.content_hash[:16] + "…" if len(g.content_hash) > 16 else g.content_hash
            lines.append(
                f"### 组 {gi} · `{algo_label.lower().replace('-', '')}:{short_h}`"
            )
            lines.append("")
            lines.append(
                f"- 内容大小: {g.size_bytes:,} bytes ({_human_bytes(g.size_bytes)})"
            )
            lines.append(
                f"- 完整哈希: `{g.content_hash}`"
            )
            lines.append(
                f"- 包含 {len(g.files)} 个文件："
            )
            for fi, sf in enumerate(g.files):
                tag = "[MAIN]" if fi == 0 else "[DUP] "
                lines.append(
                    f"  {fi+1}. `{tag}` `{sf.rel_path}` — file_id=`{sf.file_id}`"
                )
            lines.append("")

    if rep.unhashed:
        lines.append("### ⚠️ 未参与查重的文件")
        lines.append("")
        lines.append("| # | 路径 | 大小 | 跳过原因 |")
        lines.append("|---|------|------|----------|")
        for i, sf in enumerate(rep.unhashed, start=1):
            rel = str(sf.rel_path).replace("|", "\\|")
            reason = (sf.content_hash_skipped_reason or "未知").replace("|", "\\|")
            lines.append(f"| {i} | {rel} | {sf.size_bytes:,} | {reason} |")
        lines.append("")

    lines.append("---")
    lines.append("")
    return "\n".join(lines)


def _human_bytes(n: int) -> str:
    """字节数转人类可读。"""
    if n < 0:
        return f"{n} B"
    units = ["B", "KB", "MB", "GB", "TB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:,.1f} {u}" if u != "B" else f"{int(f):,} {u}"
        f /= 1024
    return f"{n} B"
