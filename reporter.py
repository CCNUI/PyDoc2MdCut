"""生成 conversion_report.md。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from config import AppConfig
from dedup import DupReport
from merger import KIND_LABEL, FileEntry
from scanner import ScannedFile


@dataclass
class OcrStats:
    call_count: int = 0
    total_chars: int = 0
    total_seconds: float = 0.0


def write_report(
    *,
    cfg: AppConfig,
    entries: list[FileEntry],
    volume_paths: list[Path],
    ocr_stats: OcrStats,
    report_path: Path,
    filtered_out: list[ScannedFile] | None = None,
    filtered_by_size: list[ScannedFile] | None = None,
    filtered_by_ext: list[ScannedFile] | None = None,
    filtered_by_ctime: list[ScannedFile] | None = None,
    all_files: list[ScannedFile] | None = None,
    dup_report: DupReport | None = None,
) -> None:
    filtered_out = filtered_out or []
    filtered_by_size = filtered_by_size or []
    filtered_by_ext = filtered_by_ext or []
    filtered_by_ctime = filtered_by_ctime or []
    all_files = all_files or []

    total = len(entries)
    success = [e for e in entries if e.result.status == "success"]
    failed = [e for e in entries if e.result.status == "failed"]
    skipped = [e for e in entries if e.result.status == "skipped"]
    total_bytes = sum(e.scanned.size_bytes for e in entries)

    image_entries = [e for e in entries if e.scanned.kind == "image"]
    ocr_success = [e for e in image_entries if e.result.status == "success"]
    scanned_pdfs = [
        e for e in entries
        if e.scanned.kind == "pdf" and e.result.extra.get("is_scanned_pdf")
    ]

    # 日志类（log/tlog/rlog）—— 抽出实际触发了截断的（兼容旧的 extra）
    log_kinds = {"log", "tlog", "rlog"}
    log_entries = [e for e in entries if e.scanned.kind in log_kinds]
    truncated_logs = [e for e in log_entries if e.result.extra.get("truncated")]

    # 新：所有触发了 post-convert 截取的条目
    post_truncated = [
        e for e in entries if e.result.extra.get("post_truncation", {}).get("truncated")
    ]

    lines: list[str] = []
    lines.append("# 🧾 Conversion Report\n")
    lines.append("")
    lines.append("## 📊 总览\n")
    if all_files:
        lines.append(f"- 📚 枚举到的文件总数（不受任何筛选影响）: {len(all_files)}")
    lines.append(f"- 进入提取流水线的文件数: {total}")
    lines.append(f"- ✅ 成功: {len(success)}")
    lines.append(f"- ❌ 失败: {len(failed)}")
    lines.append(f"- 🚫 跳过: {len(skipped)}")
    if filtered_by_ext:
        lines.append(f"- 🚷 进目录扩展名 / kind 筛选拒绝: {len(filtered_by_ext)}")
    if filtered_by_size:
        lines.append(f"- 📏 按大小过滤掉: {len(filtered_by_size)}")
    if cfg.mtime_filter_enabled or filtered_out:
        lines.append(f"- ⏰ 按修改日期过滤掉: {len(filtered_out)}")
    if cfg.ctime_filter_enabled or filtered_by_ctime:
        lines.append(f"- 📅 按创建日期过滤掉: {len(filtered_by_ctime)}")
    lines.append(f"- 总字节数（进入提取）: {total_bytes:,}")
    lines.append(f"- OCR 启用: {'是' if cfg.ocr_enabled else '否'}"
                 + ("（CLI --no-ocr 强制关闭）" if cfg.no_ocr_cli else ""))
    lines.append(f"- OCR 引擎: {cfg.ocr_engine if cfg.ocr_enabled else '未启用'}")
    lines.append(f"- OCR 调用次数: {ocr_stats.call_count}")
    lines.append(f"- OCR 总耗时: {ocr_stats.total_seconds:.2f} s")
    lines.append(f"- OCR 总识别字符: {ocr_stats.total_chars}")
    lines.append(f"- 日志单文件截取上限: {cfg.log_file_max_bytes:,} bytes（仅影响 log/tlog/rlog）")
    lines.append(f"- 通用截取启用: {'是' if cfg.truncation_enabled else '否'}（详情见下方章节）")
    lines.append(f"- 中间产物保留: {'是（' + str(cfg.intermediate_dir) + '）' if cfg.keep_intermediate else '否'}")
    if cfg.file_metadata_enabled:
        lines.append(
            f"- 文件元信息附注: 启用（位置=`{cfg.file_metadata_position}`，"
            f"算法={cfg.file_hash_algo}）"
        )
    else:
        lines.append("- 文件元信息附注: 未启用")
    if cfg.duplicate_detection_enabled and dup_report is not None:
        lines.append(
            f"- 重复文件检测: 启用 — {len(dup_report.groups)} 组重复 / "
            f"{dup_report.total_redundant_files} 个冗余副本 / "
            f"冗余 {dup_report.total_redundant_bytes:,} bytes"
        )
    elif cfg.duplicate_detection_enabled:
        lines.append("- 重复文件检测: 启用（无报告数据）")
    else:
        lines.append("- 重复文件检测: 未启用")
    lines.append("")

    # ⏰ mtime 过滤掉的列表
    if cfg.mtime_filter_enabled:
        lines.append("## ⏰ 修改日期过滤\n")
        lines.append(
            f"- 过滤范围: after = `{cfg.mtime_after or '不限'}` ；"
            f"before = `{cfg.mtime_before or '不限'}`\n"
        )
        if not filtered_out:
            lines.append("_（无文件被该筛选规则排除）_\n")
        else:
            lines.append("| # | 路径 | 修改时间 | 大小 | 类型 |")
            lines.append("|---|------|----------|------|------|")
            for i, sf in enumerate(filtered_out, start=1):
                rel = str(sf.rel_path).replace("|", "\\|")
                mt = sf.mtime.isoformat(timespec="seconds") if sf.mtime else "?"
                kind_cn = KIND_LABEL.get(sf.kind, sf.kind)
                lines.append(f"| {i} | {rel} | {mt} | {sf.size_bytes:,} | {kind_cn} |")
            lines.append("")

    # 🔁 查重摘要（与 TOC 后的内嵌报告内容互补：这里更精炼，便于人快速看）
    if cfg.duplicate_detection_enabled and dup_report is not None:
        lines.append("## 🔁 重复文件检测摘要\n")
        lines.append(f"- 哈希算法: {cfg.file_hash_algo}")
        lines.append(f"- 重复组数: {len(dup_report.groups)}")
        lines.append(
            f"- 涉及文件数: {dup_report.total_files_in_dups}（其中冗余副本 "
            f"{dup_report.total_redundant_files}）"
        )
        lines.append(f"- 冗余字节数: {dup_report.total_redundant_bytes:,}")
        if dup_report.unhashed:
            lines.append(f"- 未参与查重: {len(dup_report.unhashed)} 个文件")
        lines.append("")
        if not dup_report.groups:
            lines.append("_（未发现重复文件）_\n")
        else:
            lines.append("| 组 | 主副本 | 副本数 | 内容大小 | 冗余字节 |")
            lines.append("|----|--------|--------|----------|----------|")
            for gi, g in enumerate(dup_report.groups, start=1):
                main_rel = str(g.main.rel_path).replace("|", "\\|")
                lines.append(
                    f"| {gi} | {main_rel} | {len(g.duplicates)} | "
                    f"{g.size_bytes:,} | {g.redundant_bytes:,} |"
                )
            lines.append("")
            lines.append(
                "> 详细分组（含每个 file_id）见每卷 `merged_part_*.md` 顶部 "
                "TOC 之后的「🔁 重复文件报告」一节。\n"
            )

    # ✅ 成功列表
    lines.append("## ✅ 成功列表\n")
    if not success:
        lines.append("_（无）_\n")
    else:
        lines.append("| # | 路径 | 类型 | 转换字符数 | 备注 |")
        lines.append("|---|------|------|------------|------|")
        for i, e in enumerate(success, start=1):
            kind_cn = KIND_LABEL.get(e.scanned.kind, e.scanned.kind)
            chars = e.result.extra.get("chars") or e.result.extra.get("ocr_chars") or 0
            note_parts = []
            if e.scanned.kind == "image":
                note_parts.append(f"OCR 字符 {e.result.extra.get('ocr_chars', 0)}")
                note_parts.append(f"耗时 {e.result.extra.get('ocr_elapsed', 0):.2f}s")
            if e.scanned.kind == "param":
                pc = e.result.extra.get("param_count", 0)
                note_parts.append(f"参数 {pc}")
            if e.scanned.kind in log_kinds and e.result.extra.get("truncated"):
                note_parts.append(
                    f"已截断 {e.result.extra.get('skipped_bytes', 0):,} B"
                )
            if e.scanned.kind == "tlog":
                eng = e.result.extra.get("engine", "")
                if eng:
                    note_parts.append(f"engine={eng}")
                msgs = e.result.extra.get("messages_parsed")
                if msgs is not None:
                    note_parts.append(f"消息 {msgs:,}")
            if e.scanned.kind == "wps":
                eng = e.result.extra.get("engine", "")
                if eng:
                    note_parts.append(f"engine={eng}")
            if e.result.warnings:
                note_parts.append(f"警告 {len(e.result.warnings)} 条")
            note = "; ".join(note_parts) or ""
            rel = str(e.scanned.rel_path).replace("|", "\\|")
            lines.append(f"| {i} | {rel} | {kind_cn} | {chars} | {note} |")
        lines.append("")

    # ⚠️ 图片型 PDF
    lines.append("## ⚠️ 图片型 PDF 列表\n")
    if not scanned_pdfs:
        lines.append("_（无）_\n")
    else:
        lines.append("| # | 路径 | 页数 | 提取字符数 |")
        lines.append("|---|------|------|------------|")
        for i, e in enumerate(scanned_pdfs, start=1):
            rel = str(e.scanned.rel_path).replace("|", "\\|")
            pages = e.result.extra.get("pages", "?")
            chars = e.result.extra.get("chars", 0)
            lines.append(f"| {i} | {rel} | {pages} | {chars} |")
        lines.append("")
        lines.append(
            "> 提示：这些 PDF 提取的文字几乎为空，建议用 OCR 工具（如 Tesseract、"
            "ABBYY FineReader、或将每页渲染成图片走百度 OCR）后再合并。\n"
        )

    # 📷 OCR 图片
    lines.append("## 📷 OCR 图片列表\n")
    if not ocr_success:
        lines.append("_（无 OCR 成功的图片）_\n")
    else:
        lines.append("| # | 路径 | 大小 | 字符数 | 耗时(s) | 引擎 |")
        lines.append("|---|------|------|--------|---------|------|")
        for i, e in enumerate(ocr_success, start=1):
            rel = str(e.scanned.rel_path).replace("|", "\\|")
            size = e.result.extra.get("size", 0)
            chars = e.result.extra.get("ocr_chars", 0)
            elapsed = e.result.extra.get("ocr_elapsed", 0.0)
            engine = e.result.extra.get("ocr_engine", "?")
            lines.append(f"| {i} | {rel} | {size} B | {chars} | {elapsed:.2f} | {engine} |")
        lines.append("")

    # ✂️ 日志体积截断
    lines.append("## ✂️ 日志类文件截取（兼容旧 LOG_FILE_MAX_BYTES）\n")
    if not log_entries:
        lines.append("_（本次没有 .log / .tlog / .rlog 文件）_\n")
    else:
        lines.append(
            f"- 单文件截取上限: `LOG_FILE_MAX_BYTES = {cfg.log_file_max_bytes:,}` bytes\n"
            f"- 头部占比: `LOG_TRUNCATE_HEAD_RATIO = {cfg.log_truncate_head_ratio}`\n"
        )
        if not truncated_logs:
            lines.append("_（无文件触发截断）_\n")
        else:
            lines.append("| # | 路径 | 类型 | 原始大小 | 头部 | 尾部 | 舍弃 |")
            lines.append("|---|------|------|----------|------|------|------|")
            for i, e in enumerate(truncated_logs, start=1):
                rel = str(e.scanned.rel_path).replace("|", "\\|")
                kind_cn = KIND_LABEL.get(e.scanned.kind, e.scanned.kind)
                ob = e.result.extra.get("original_bytes", 0)
                hb = e.result.extra.get("head_bytes", 0)
                tb = e.result.extra.get("tail_bytes", 0)
                sb = e.result.extra.get("skipped_bytes", 0)
                lines.append(
                    f"| {i} | {rel} | {kind_cn} | {ob:,} | {hb:,} | {tb:,} | {sb:,} |"
                )
            lines.append("")

    # ✂️ 通用截取（按文件类型独立配置）
    lines.append("## ✂️ 通用截取（TRUNC_*：头+尾+中间随机块）\n")
    lines.append(f"- 总开关: {'启用' if cfg.truncation_enabled else '禁用'}")
    if cfg.truncation_enabled and cfg.truncation_catalog is not None:
        lines.append(f"- 随机种子: {cfg.truncation_seed}（基于文件名 + 种子，结果可复现）")
        lines.append("- 各类型规格:")
        try:
            for desc in cfg.truncation_catalog.known_keys_describe():
                lines.append(f"  {desc.strip()}")
        except Exception:
            pass
        lines.append("")
        if not post_truncated:
            lines.append("_（无文件触发通用截取）_\n")
        else:
            lines.append(
                "| # | 路径 | 类型 | 原始 | 保留 | 头 | 尾 | 中间 | 中块数 | 规格 |"
            )
            lines.append(
                "|---|------|------|------|------|----|----|------|--------|------|"
            )
            for i, e in enumerate(post_truncated, start=1):
                rel = str(e.scanned.rel_path).replace("|", "\\|")
                kind_cn = KIND_LABEL.get(e.scanned.kind, e.scanned.kind)
                pt = e.result.extra.get("post_truncation", {})
                lines.append(
                    f"| {i} | {rel} | {kind_cn} | "
                    f"{pt.get('original_bytes', 0):,} | "
                    f"{pt.get('kept_bytes', 0):,} | "
                    f"{pt.get('head_bytes', 0):,} | "
                    f"{pt.get('tail_bytes', 0):,} | "
                    f"{pt.get('middle_bytes', 0):,} | "
                    f"{pt.get('middle_chunks_kept', 0)}/{pt.get('middle_chunks_requested', 0)} | "
                    f"{pt.get('spec', '')} |"
                )
            lines.append("")
    else:
        lines.append("")

    # 📏 按大小过滤
    lines.append("## 📏 按大小过滤\n")
    lines.append(
        f"- 总开关: {'启用' if cfg.size_filter_enabled else '禁用'}"
    )
    if cfg.size_filter_enabled and cfg.size_filter_catalog is not None:
        lines.append("- 各类型规格:")
        try:
            for desc in cfg.size_filter_catalog.known_keys_describe():
                lines.append(f"  {desc.strip()}")
        except Exception:
            pass
        lines.append("")
        if not filtered_by_size:
            lines.append("_（无文件被大小筛选排除）_\n")
        else:
            lines.append("| # | 路径 | 类型 | 大小 | 原因 |")
            lines.append("|---|------|------|------|------|")
            for i, sf in enumerate(filtered_by_size, start=1):
                rel = str(sf.rel_path).replace("|", "\\|")
                kind_cn = KIND_LABEL.get(sf.kind, sf.kind)
                reason = sf.content_hash_skipped_reason or "size_filter"
                if reason.startswith("size_filter: "):
                    reason = reason[len("size_filter: "):]
                lines.append(
                    f"| {i} | {rel} | {kind_cn} | {sf.size_bytes:,} B | {reason} |"
                )
            lines.append("")
    else:
        lines.append("")

    # ❌ 失败列表
    lines.append("## ❌ 失败列表\n")
    if not failed:
        lines.append("_（无）_\n")
    else:
        lines.append("| # | 路径 | 类型 | 异常摘要 |")
        lines.append("|---|------|------|----------|")
        for i, e in enumerate(failed, start=1):
            kind_cn = KIND_LABEL.get(e.scanned.kind, e.scanned.kind)
            err = (e.result.error or "").replace("|", "\\|").replace("\n", " ")
            rel = str(e.scanned.rel_path).replace("|", "\\|")
            lines.append(f"| {i} | {rel} | {kind_cn} | {err} |")
        lines.append("")

    # 🚫 跳过列表
    lines.append("## 🚫 跳过列表\n")
    if not skipped:
        lines.append("_（无）_\n")
    else:
        lines.append("| # | 路径 | 类型 | 跳过原因 |")
        lines.append("|---|------|------|----------|")
        for i, e in enumerate(skipped, start=1):
            kind_cn = KIND_LABEL.get(e.scanned.kind, e.scanned.kind)
            reason = (e.result.skip_reason or "未指定").replace("|", "\\|")
            rel = str(e.scanned.rel_path).replace("|", "\\|")
            lines.append(f"| {i} | {rel} | {kind_cn} | {reason} |")
        lines.append("")

    # 📦 分卷清单
    lines.append("## 📦 分卷清单\n")
    if not volume_paths:
        lines.append("_（未生成分卷文件）_\n")
    else:
        lines.append("| 卷号 | 文件名 | 字节数 |")
        lines.append("|------|--------|--------|")
        for i, p in enumerate(volume_paths, start=1):
            try:
                sz = p.stat().st_size
            except OSError:
                sz = 0
            lines.append(f"| {i} | {p.name} | {sz:,} |")
        lines.append("")

    # 🚷 进目录扩展名 / kind 筛选 拒绝清单
    lines.append("## 🚷 进目录扩展名 / kind 筛选\n")
    ef = getattr(cfg, "eligibility_ext_filter", None)
    if ef is not None:
        lines.append(f"- 规则: {ef.describe()}")
    if not filtered_by_ext:
        lines.append("_（无文件被 ext_filter 拒绝）_\n")
    else:
        lines.append("| # | 路径 | 类型 | 大小 | 原因 |")
        lines.append("|---|------|------|------|------|")
        for i, sf in enumerate(filtered_by_ext, start=1):
            rel = str(sf.rel_path).replace("|", "\\|")
            kind_cn = KIND_LABEL.get(sf.kind, sf.kind)
            reasons = [r for r in sf.eligibility_reasons if r.startswith("ext_filter")]
            reason_str = "；".join(r.replace("ext_filter: ", "") for r in reasons).replace("|", "\\|")
            lines.append(f"| {i} | {rel} | {kind_cn} | {sf.size_bytes:,} B | {reason_str} |")
        lines.append("")

    # 📅 ctime 筛选拒绝清单
    if cfg.ctime_filter_enabled or filtered_by_ctime:
        lines.append("## 📅 按创建日期过滤\n")
        if not filtered_by_ctime:
            lines.append("_（无文件被 ctime 过滤排除）_\n")
        else:
            lines.append("| # | 路径 | 类型 | 创建时间 | 来源 | 原因 |")
            lines.append("|---|------|------|----------|------|------|")
            for i, sf in enumerate(filtered_by_ctime, start=1):
                rel = str(sf.rel_path).replace("|", "\\|")
                kind_cn = KIND_LABEL.get(sf.kind, sf.kind)
                ct = sf.ctime.isoformat(timespec="seconds") if sf.ctime else "-"
                reasons = [r for r in sf.eligibility_reasons if r.startswith("ctime_filter")]
                reason_str = "；".join(r.replace("ctime_filter: ", "") for r in reasons).replace("|", "\\|")
                lines.append(
                    f"| {i} | {rel} | {kind_cn} | {ct} | {sf.ctime_source} | {reason_str} |"
                )
            lines.append("")

    # 📚 完整文件目录（全量枚举）— 不受任何筛选影响
    if getattr(cfg, "full_inventory_in_report", True) and all_files:
        try:
            from merger import render_full_inventory
            lines.append(render_full_inventory(all_files))
        except Exception as e:
            lines.append(f"## 📚 完整文件目录\n\n_（渲染失败: {e}）_\n")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
