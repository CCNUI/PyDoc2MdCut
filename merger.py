"""合并模块：把所有转换结果拼成一个大 markdown（含 TOC、分隔符）。

不在这里做分卷；分卷由 splitter 处理。

输出结构：
1. 文件头（标题 + 元信息）
2. 目录 TOC （元信息可选附在每条之后）
3. 重复文件报告（如启用查重；放在 TOC 之后）
4. 每个文件的 FILE START/END 块 （元信息可选附在文件头）
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from config import AppConfig
from converters.base import ConversionResult
from dedup import DupReport, render_dup_report_markdown
from scanner import ScannedFile


# 类型 → 中文显示名
KIND_LABEL = {
    "pdf": "PDF",
    "docx": "Word",
    "txt": "纯文本",
    "csv": "CSV",
    "xlsx": "Excel",
    "json": "JSON",
    "markdown": "Markdown",
    "html": "HTML",
    "image": "图片",
    # 新增
    "param": "MissionPlanner 参数",
    "log": "通用日志",
    "rlog": "rlog 原始日志",
    "tlog": "MAVLink 遥测",
    "wps": "金山 WPS",
    # 旧版 Office (OLE 复合二进制)
    "doc": "Word 97-2003",
    "xls": "Excel 97-2003",
    "ppt": "PowerPoint 97-2003",
    # 代码 / 配置
    "code_py": "Python", "code_sh": "Shell", "code_js": "JavaScript",
    "code_ts": "TypeScript", "code_java": "Java", "code_c": "C", "code_cpp": "C++",
    "code_cs": "C#", "code_go": "Go", "code_rs": "Rust", "code_rb": "Ruby",
    "code_php": "PHP", "code_swift": "Swift", "code_kt": "Kotlin", "code_scala": "Scala",
    "code_m": "Objective-C", "code_mm": "Objective-C++", "code_r": "R",
    "code_sql": "SQL", "code_yaml": "YAML", "code_toml": "TOML", "code_ini": "INI",
    "code_xml": "XML", "code_css": "CSS", "code_dockerfile": "Dockerfile",
    # 可执行文件
    "exe": "Windows PE",
    # 压缩包
    "archive_zip": "ZIP 压缩包",
    "archive_tar": "TAR 压缩包",
    "archive_7z": "7-Zip 压缩包",
    "archive_rar": "RAR 压缩包",
    "archive_gz": "Gzip 压缩包",
    "unsupported": "不支持",
}

_ALGO_LABEL = {
    "sha256": "SHA-256",
    "sha1": "SHA-1",
    "md5": "MD5",
}


@dataclass
class FileEntry:
    """合并阶段使用：每个文件的扫描信息 + 转换结果。"""
    scanned: ScannedFile
    result: ConversionResult


# ----------------------------------------------------------------------------
# 元信息格式化辅助
# ----------------------------------------------------------------------------

def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    units = ["KB", "MB", "GB", "TB"]
    f = float(n) / 1024
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:,.1f} {u}"
        f /= 1024
    return f"{n} B"


def _ctime_label(source: str) -> str:
    return {
        "birthtime": "创建时间",
        "win_ctime": "创建时间",
        "status_ctime": "创建时间(*近似)",
        "unavailable": "创建时间",
    }.get(source, "创建时间")


def _ctime_note(source: str) -> str | None:
    """对于不可靠的 ctime 来源给一个简短解释（仅用于 verbose body 形式）。"""
    if source == "status_ctime":
        return "仅为元数据变更时间，文件系统未提供真创建时间"
    if source == "unavailable":
        return "不可用"
    return None


def _algo_label(algo: str | None) -> str:
    if not algo:
        return "HASH"
    return _ALGO_LABEL.get(algo, algo.upper())


def render_full_inventory(
    all_files: list[ScannedFile],
    *,
    eligible_count: int | None = None,
) -> str:
    """渲染「完整文件目录」清单。

    作用：给 LLM 一个全量目录视图，含 byte 精度大小和数量级标签，便于查重 / 引用。
    特点：
      - **不受任何筛选影响**：所有枚举到的文件都在这里
      - 大小用精确字节数；同时附 size_class（<1KB / 1KB-1MB / ... / >100MB）
      - 标注哪些文件被「进目录筛选」拒绝（eligible=False）以及拒绝原因
      - 在最后给一份 TSV 块，方便 LLM 直接 parse
    """
    n_total = len(all_files)
    if eligible_count is None:
        eligible_count = sum(1 for f in all_files if f.eligible)
    n_rejected = n_total - eligible_count

    lines: list[str] = []
    lines.append("## 📚 完整文件目录（全量枚举，不受筛选影响）\n")
    lines.append(
        "> 此清单列出输入目录中**所有**被扫描到的文件，"
        "**不受**扩展名 / 大小 / mtime / ctime 任何筛选影响。\n"
    )
    lines.append(
        "> 文件大小以 **byte** 精确给出（不四舍五入），"
        "便于按 (path, size_bytes) 组合做查重——超过 10MB 的文件，"
        "精确字节几乎是唯一指纹。\n"
    )
    lines.append(
        f"> 统计：共 **{n_total}** 个文件；其中 **{eligible_count}** 个有资格进入提取，"
        f"**{n_rejected}** 个被进目录筛选拒绝。\n"
    )
    lines.append("")

    # 表格视图（人类可读）
    lines.append("| # | 路径 | 类型 | size_bytes | size_class | mtime | ctime | 资格 | 拒因 |")
    lines.append("|---|------|------|-----------:|------------|-------|-------|:----:|------|")
    for i, sf in enumerate(all_files, start=1):
        rel = str(sf.rel_path).replace("|", "\\|")
        kind_cn = KIND_LABEL.get(sf.kind, sf.kind)
        mt = sf.mtime.isoformat(timespec="seconds") if sf.mtime else "-"
        if sf.ctime:
            ct = sf.ctime.isoformat(timespec="seconds")
            if sf.ctime_source == "status_ctime":
                ct += "*"
            elif sf.ctime_source == "unavailable":
                ct = "-"
        else:
            ct = "-"
        flag = "✅" if sf.eligible else "❌"
        reasons = "；".join(sf.eligibility_reasons) if not sf.eligible else ""
        reasons = reasons.replace("|", "\\|")
        lines.append(
            f"| {i} | {rel} | {kind_cn} | {sf.size_bytes:,} | {sf.size_class} | "
            f"{mt} | {ct} | {flag} | {reasons} |"
        )
    lines.append("")

    # 机器友好 TSV 块（让 LLM / 脚本 parse 起来更稳）
    lines.append("### 机器可读索引（TSV）\n")
    lines.append("```tsv")
    lines.append("idx\tfile_id\trel_path\tkind\tsize_bytes\tsize_class\tmtime\tctime\tctime_source\teligible\treasons")
    for i, sf in enumerate(all_files, start=1):
        mt = sf.mtime.isoformat(timespec="seconds") if sf.mtime else ""
        ct = sf.ctime.isoformat(timespec="seconds") if sf.ctime else ""
        rs = "; ".join(sf.eligibility_reasons)
        # TSV 内禁止 tab/newline
        rel = str(sf.rel_path).replace("\t", " ").replace("\n", " ")
        rs = rs.replace("\t", " ").replace("\n", " ")
        lines.append(
            f"{i}\t{sf.file_id}\t{rel}\t{sf.kind}\t{sf.size_bytes}\t{sf.size_class}\t"
            f"{mt}\t{ct}\t{sf.ctime_source}\t{int(sf.eligible)}\t{rs}"
        )
    lines.append("```\n")
    return "\n".join(lines)


def format_metadata_compact(cfg: AppConfig, sf: ScannedFile) -> str:
    """返回紧凑单行元信息（用于 TOC 后追加）。

    例: `1.2 MB · m:2025-03-15 · b:2025-03-10 · sha256:a1b2c3d4`
    任何子项关闭就跳过；全部关闭返回空字符串。
    """
    parts: list[str] = []
    if cfg.file_metadata_show_size:
        parts.append(_human_bytes(sf.size_bytes))
    if cfg.file_metadata_show_mtime and sf.mtime:
        parts.append(f"m:{sf.mtime.strftime('%Y-%m-%d')}")
    if cfg.file_metadata_show_ctime:
        if sf.ctime and sf.ctime_source in ("birthtime", "win_ctime"):
            parts.append(f"b:{sf.ctime.strftime('%Y-%m-%d')}")
        elif sf.ctime and sf.ctime_source == "status_ctime":
            # 用 *c: 加星号提示这不是真创建时间
            parts.append(f"*c:{sf.ctime.strftime('%Y-%m-%d')}")
        # unavailable 直接不显示，避免混淆
    if cfg.file_metadata_show_hash and sf.content_hash:
        algo = sf.content_hash_algo or cfg.file_hash_algo
        short = sf.content_hash[: cfg.file_hash_short_len]
        parts.append(f"{algo}:{short}")
    elif cfg.file_metadata_show_hash and sf.content_hash_skipped_reason:
        parts.append(f"{cfg.file_hash_algo}:n/a")
    return " · ".join(parts)


def format_metadata_verbose(cfg: AppConfig, sf: ScannedFile) -> list[str]:
    """返回 verbose 多行元信息（用于正文 FILE START 块）。

    每条都已格式化为 markdown bullet 行，例：
      "- 修改时间: 2025-03-15 14:23:01"
    全部关闭则返回 []。
    """
    out: list[str] = []
    if cfg.file_metadata_show_size:
        out.append(
            f"- 文件大小: {sf.size_bytes:,} bytes ({_human_bytes(sf.size_bytes)})"
        )
    if cfg.file_metadata_show_mtime:
        if sf.mtime:
            out.append(f"- 修改时间: {sf.mtime.isoformat(timespec='seconds')}")
        else:
            out.append("- 修改时间: (不可用)")
    if cfg.file_metadata_show_ctime:
        label = _ctime_label(sf.ctime_source)
        if sf.ctime and sf.ctime_source in ("birthtime", "win_ctime"):
            out.append(f"- {label}: {sf.ctime.isoformat(timespec='seconds')}")
        elif sf.ctime and sf.ctime_source == "status_ctime":
            note = _ctime_note(sf.ctime_source)
            out.append(
                f"- {label}: {sf.ctime.isoformat(timespec='seconds')}"
                + (f"  ({note})" if note else "")
            )
        else:
            out.append(f"- {label}: (不可用：本文件系统不提供真实创建时间)")
    if cfg.file_metadata_show_hash:
        if sf.content_hash:
            algo = _algo_label(sf.content_hash_algo)
            out.append(f"- {algo}: `{sf.content_hash}`")
        elif sf.content_hash_skipped_reason:
            algo = _algo_label(cfg.file_hash_algo)
            out.append(
                f"- {algo}: (未计算 - {sf.content_hash_skipped_reason})"
            )
    return out


def _metadata_in_toc(cfg: AppConfig) -> bool:
    return (
        cfg.file_metadata_enabled
        and cfg.file_metadata_position in ("toc", "both")
    )


def _metadata_in_body(cfg: AppConfig) -> bool:
    return (
        cfg.file_metadata_enabled
        and cfg.file_metadata_position in ("body", "both")
    )


# ----------------------------------------------------------------------------
# 文档头 + TOC + （可选）查重报告
# ----------------------------------------------------------------------------

def build_header(
    cfg: AppConfig,
    entries: list[FileEntry],
    *,
    dup_report: DupReport | None = None,
    all_files: list[ScannedFile] | None = None,
) -> str:
    """生成合并文件最顶部的 header（含元信息、目录、可选查重报告）。

    - all_files: 全量枚举结果。如果 cfg.full_inventory_in_merged 为 True，
      会在 TOC 之前插入「完整文件目录」章节。
    """
    total = len(entries)
    success = sum(1 for e in entries if e.result.status == "success")
    failed = sum(1 for e in entries if e.result.status == "failed")
    skipped = sum(1 for e in entries if e.result.status == "skipped")
    total_bytes = sum(e.scanned.size_bytes for e in entries)

    ocr_status_cn = "是" if cfg.ocr_enabled else "否"
    ocr_engine_cn = cfg.ocr_engine if cfg.ocr_enabled else "未启用"

    lines: list[str] = []
    lines.append("# 📚 Merged Document Bundle\n")
    lines.append("")
    lines.append(f"- 生成时间: {dt.datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"- 源目录: {cfg.input_dir}")
    if all_files is not None:
        lines.append(f"- 枚举到的文件总数: {len(all_files)}")
    lines.append(f"- 进入提取的文件数: {total}")
    lines.append(f"- 成功: {success}  失败: {failed}  跳过: {skipped}")
    lines.append(f"- 总字节数（进入提取）: {total_bytes:,}")
    lines.append(f"- OCR 启用: {ocr_status_cn}")
    lines.append(f"- OCR 引擎: {ocr_engine_cn}")
    if cfg.file_metadata_enabled:
        algo = _algo_label(cfg.file_hash_algo) if cfg.file_metadata_show_hash else "-"
        lines.append(
            f"- 元信息附注: 启用（位置=`{cfg.file_metadata_position}`，"
            f"哈希算法={algo}）"
        )
    if cfg.duplicate_detection_enabled:
        if dup_report is not None:
            lines.append(
                f"- 重复文件检测: 启用 — 发现 {len(dup_report.groups)} 组重复，"
                f"冗余 {dup_report.total_redundant_files} 个文件"
            )
        else:
            lines.append("- 重复文件检测: 启用（暂无报告数据）")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 完整文件目录（如启用且提供）
    if (
        all_files is not None
        and getattr(cfg, "full_inventory_in_merged", True)
    ):
        lines.append(render_full_inventory(
            all_files,
            eligible_count=sum(1 for f in all_files if f.eligible),
        ))
        lines.append("---")
        lines.append("")

    lines.append("## 📑 目录 (Table of Contents)")
    lines.append("")
    if _metadata_in_toc(cfg):
        # 给一行简短的格式说明，让 LLM 知道 TOC 行末尾的紧凑串怎么读
        lines.append(
            "> 每行末尾的紧凑信息含义： `大小 · m:修改日期 · "
            "b:创建日期(birthtime) / *c:近似创建日期 · "
            f"{cfg.file_hash_algo}:哈希前缀`"
        )
        lines.append("")

    in_toc = _metadata_in_toc(cfg)
    for i, e in enumerate(entries, start=1):
        kind_cn = KIND_LABEL.get(e.scanned.kind, e.scanned.kind)
        status_cn = {"success": "✅成功", "failed": "❌失败", "skipped": "🚫跳过"}.get(
            e.result.status, e.result.status
        )
        # 这里目录使用文件 ID 作为锚点；很多 LLM 不依赖锚点跳转，但保留链接形式更友好
        anchor = f"file-{e.scanned.file_id}"
        base = (
            f"{i}. [{e.scanned.rel_path}](#{anchor}) — {kind_cn} — {status_cn}"
        )
        if in_toc:
            meta = format_metadata_compact(cfg, e.scanned)
            if meta:
                base += f" — `{meta}`"
        lines.append(base)
    lines.append("")
    lines.append("---")
    lines.append("")

    # 查重报告：放 TOC 之后
    if cfg.duplicate_detection_enabled and dup_report is not None:
        lines.append(render_dup_report_markdown(dup_report))
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# 单文件正文块
# ----------------------------------------------------------------------------

def build_file_block(entry: FileEntry, cfg: AppConfig | None = None) -> str:
    """生成单文件的完整块（含前后分隔符）。

    cfg 为 None 时按"无元信息"渲染，向后兼容。
    """
    s = entry.scanned
    r = entry.result
    rel = str(s.rel_path)
    kind_cn = KIND_LABEL.get(s.kind, s.kind)
    fid = s.file_id

    # 警告字符串
    if r.warnings:
        warn_str = "; ".join(r.warnings)
    else:
        warn_str = "无"

    # 状态对应的占位/正文
    if r.status == "success":
        body = r.markdown
    elif r.status == "failed":
        # 占位 + 用户给的正文（image_converter 会把失败信息也写到 markdown 里）
        placeholder = (
            f"> ❌ **此文件转换失败**：{r.error or '未知错误'}\n"
            f"> 已跳过内容，仅保留占位。\n"
        )
        body = placeholder + (r.markdown if r.markdown else "")
    else:  # skipped
        placeholder = (
            f"> 🚫 **此文件已跳过**：{r.skip_reason or '未指定原因'}\n"
        )
        body = (r.markdown if r.markdown else placeholder)

    if not body.endswith("\n"):
        body += "\n"

    # 锚点：放一个隐藏的 anchor，方便 TOC 跳转
    anchor_html = f'<a id="file-{fid}"></a>\n'

    header_lines = [
        f"===== FILE START: {rel} =====\n",
        f"===== FILE ID: {fid} =====\n",
        f"- 类型: {kind_cn}\n",
        f"- 原始大小: {s.size_bytes} bytes\n",
    ]
    # 元信息（verbose 形式追加在这里）
    if cfg is not None and _metadata_in_body(cfg):
        meta_lines = format_metadata_verbose(cfg, s)
        for ml in meta_lines:
            header_lines.append(ml + "\n")
    # TOC 与 body 都开时，body 已含完整信息；只开 TOC 时，TOC 是紧凑串。
    # 这里也给出"紧凑串"形式以便单卷里读到 file 块时也能一眼看到 hash —— 仅
    # 在 position=toc（此处 _metadata_in_body 假，但启用且不是 none）的时候，
    # 为帮助 LLM 在正文中仍能定位 hash，追加一行注释式紧凑串。
    if (
        cfg is not None
        and cfg.file_metadata_enabled
        and cfg.file_metadata_position == "toc"
    ):
        compact = format_metadata_compact(cfg, s)
        if compact:
            header_lines.append(f"<!-- meta: {compact} -->\n")
    header_lines.append(f"- 转换警告: {warn_str}\n")

    parts = [
        "\n\n",
        "<!-- ================================================================ -->\n",
        f"<!-- ============ FILE START · ID={fid} ============ -->\n",
        f"<!-- ============ PATH: {rel} ============ -->\n",
        "<!-- ================================================================ -->\n",
        "\n",
        anchor_html,
        *header_lines,
        "\n",
        body,
        "\n",
        f"===== FILE END: {rel} =====\n",
        f"===== FILE ID: {fid} =====\n",
        "\n",
        "<!-- ================================================================ -->\n",
        f"<!-- ============ FILE END · ID={fid} ============ -->\n",
        f"<!-- ============ PATH: {rel} ============ -->\n",
        "<!-- ================================================================ -->\n",
        "\n\n",
    ]
    return "".join(parts)

