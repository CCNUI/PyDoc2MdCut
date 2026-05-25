"""CLI 入口。

用法示例:
    python convert.py --input ./my_docs --output ./out --max-size-mb 19
    python convert.py --input ./my_docs --output ./out --no-ocr
    python convert.py --input ./my_docs --output ./out --filter-mtime
    python convert.py --input ./my_docs --output ./out --keep-intermediate
    python convert.py --input ./my_docs --output ./out --verbose
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

from tqdm import tqdm

from config import AppConfig, load_config
from converters import (
    ALL_ARCHIVE_KINDS,
    ALL_CODE_KINDS,
    BaseConverter,
    CsvConverter,
    DocConverter,
    DocxConverter,
    ExeConverter,
    HtmlConverter,
    ImageConverter,
    JsonConverter,
    LogConverter,
    MarkdownConverter,
    ParamConverter,
    PdfConverter,
    PptConverter,
    RlogConverter,
    TlogConverter,
    TxtConverter,
    WpsConverter,
    XlsConverter,
    XlsxConverter,
    make_archive_converter,
    make_code_converter,
)
from converters.base import ConversionResult
from merger import FileEntry
from ocr.baidu import BaiduOCRClient
from reporter import OcrStats, write_report
from scanner import ScannedFile, scan
from splitter import split_and_write


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "扫描文件夹 → 转换为 Markdown（图片走百度 OCR）→ "
            "合并 → 按 MB 分卷输出。"
        ),
    )
    parser.add_argument(
        "--input", "-i", required=False, type=Path, default=None,
        help="输入目录（递归扫描）。不传时从 .env 的 INPUT_DIR 读取",
    )
    parser.add_argument(
        "--output", "-o", required=False, type=Path, default=None,
        help="输出目录（分卷 md + 报告写到这里）。不传时从 .env 的 OUTPUT_DIR 读取。"
             "默认会自动追加 _YYYYMMDD_HHMMSS 时间戳后缀",
    )
    # 输出时间戳后缀（默认开启）；可以通过 --no-timestamp-output 关闭
    ts_grp = parser.add_mutually_exclusive_group()
    ts_grp.add_argument(
        "--timestamp-output", dest="timestamp_output", action="store_true",
        default=None,
        help="给 --output 路径自动追加 _YYYYMMDD_HHMMSS 时间戳后缀（覆盖 .env）",
    )
    ts_grp.add_argument(
        "--no-timestamp-output", dest="timestamp_output", action="store_false",
        help="禁止给 --output 追加时间戳（覆盖 .env，原样使用用户给的路径）",
    )
    parser.add_argument(
        "--max-size-mb", type=int, default=None,
        help="单卷最大字节数（MB），覆盖 .env 的 MAX_SIZE_MB；不写则用 .env / 默认 19",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="DEBUG 级日志（覆盖 .env 的 LOG_LEVEL）",
    )
    parser.add_argument(
        "--no-ocr", action="store_true",
        help="临时强制关闭 OCR（图片文件全部跳过）",
    )
    # mtime 过滤开关：互斥
    mtime_grp = parser.add_mutually_exclusive_group()
    mtime_grp.add_argument(
        "--filter-mtime", dest="filter_mtime", action="store_true",
        default=None,
        help="启用按修改日期筛选（覆盖 .env 的 MTIME_FILTER_ENABLED）；"
             "实际起止日期仍来自 .env 的 MTIME_AFTER / MTIME_BEFORE",
    )
    mtime_grp.add_argument(
        "--no-filter-mtime", dest="filter_mtime", action="store_false",
        help="临时强制关闭 mtime 筛选（覆盖 .env）",
    )
    # 中间产物保留开关：互斥
    keep_grp = parser.add_mutually_exclusive_group()
    keep_grp.add_argument(
        "--keep-intermediate", dest="keep_intermediate", action="store_true",
        default=None,
        help="保留每个文件的转换中间产物到 <output>/intermediate/（覆盖 .env）",
    )
    keep_grp.add_argument(
        "--no-keep-intermediate", dest="keep_intermediate", action="store_false",
        help="临时强制不保留中间产物（覆盖 .env）",
    )
    # 文件元信息开关：互斥
    meta_grp = parser.add_mutually_exclusive_group()
    meta_grp.add_argument(
        "--with-metadata", dest="file_metadata", action="store_true",
        default=None,
        help="启用文件元信息附注（哈希/修改日期/创建日期/大小），覆盖 .env",
    )
    meta_grp.add_argument(
        "--no-metadata", dest="file_metadata", action="store_false",
        help="临时强制不附注元信息（覆盖 .env）",
    )
    parser.add_argument(
        "--metadata-position", dest="metadata_position",
        choices=["toc", "body", "both", "none"],
        default=None,
        help="元信息显示位置（覆盖 .env 的 FILE_METADATA_POSITION）",
    )
    # 查重开关：互斥
    dedup_grp = parser.add_mutually_exclusive_group()
    dedup_grp.add_argument(
        "--dedup", dest="dedup", action="store_true",
        default=None,
        help="启用重复文件检测，会在 TOC 后插入 AI 友好查重报告（覆盖 .env）",
    )
    dedup_grp.add_argument(
        "--no-dedup", dest="dedup", action="store_false",
        help="临时强制关闭查重（覆盖 .env）",
    )
    parser.add_argument(
        "--env", type=Path, default=Path(".env"),
        help="指定 .env 文件路径（默认: ./.env）",
    )
    return parser


def _setup_logging(cfg: AppConfig) -> None:
    """同时输出到控制台和 run.log。"""
    level = logging.DEBUG if cfg.verbose else getattr(
        logging, cfg.log_level.upper(), logging.INFO
    )
    fmt = "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    log_path = cfg.output_dir / "run.log"

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(level)

    console = logging.StreamHandler(sys.stderr)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(console)

    fileh = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    fileh.setLevel(logging.DEBUG)
    fileh.setFormatter(logging.Formatter(fmt, datefmt))
    root.addHandler(fileh)


def _build_converter_map(
    cfg: AppConfig, ocr_client: BaiduOCRClient | None
) -> dict[str, BaseConverter]:
    m: dict[str, BaseConverter] = {
        "pdf": PdfConverter(cfg),
        "docx": DocxConverter(cfg),
        "txt": TxtConverter(cfg),
        "csv": CsvConverter(cfg),
        "xlsx": XlsxConverter(cfg),
        "json": JsonConverter(cfg),
        "markdown": MarkdownConverter(cfg),
        "html": HtmlConverter(cfg),
        "image": ImageConverter(cfg, ocr_client),
        # 新增
        "param": ParamConverter(cfg),
        "log": LogConverter(cfg),
        "rlog": RlogConverter(cfg),
        "tlog": TlogConverter(cfg),
        "wps": WpsConverter(cfg),
        "exe": ExeConverter(cfg),
        # 旧版 Office (.doc/.xls/.ppt) — olefile 兜底
        "doc": DocConverter(cfg),
        "xls": XlsConverter(cfg),
        "ppt": PptConverter(cfg),
    }
    # 代码 / 配置类（.py / .sh / .yaml 等）—— 动态注册每种 code_* kind
    for code_kind in ALL_CODE_KINDS:
        m[code_kind] = make_code_converter(code_kind)(cfg)
    # 压缩包类（.zip / .7z / .rar / .tar / .gz）
    for arc_kind in ALL_ARCHIVE_KINDS:
        m[arc_kind] = make_archive_converter(arc_kind)(cfg)
    return m


def _convert_unsupported(scanned: ScannedFile) -> ConversionResult:
    md = (
        "> 🚫 **此文件类型不支持**\n"
        f"> - 文件: {scanned.rel_path}\n"
        f"> - 扩展名: {scanned.ext or '（无）'}\n"
    )
    return ConversionResult(
        status="skipped",
        markdown=md,
        skip_reason=f"不支持的扩展名: {scanned.ext or '(无)'}",
    )


# Windows / Linux 通用的安全文件名正则
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._\-+]+")


def _safe_filename(rel_path: Path, file_id: str) -> str:
    """把相对路径压成单个文件名，前缀 file_id 防冲突。"""
    flat = "_".join(rel_path.parts)
    # 太长会触发文件系统限制，截到 100 字符
    flat = _UNSAFE_NAME_RE.sub("_", flat)[:100]
    return f"{file_id}__{flat}.md"


def _save_intermediate(cfg: AppConfig, sf: ScannedFile, result: ConversionResult) -> None:
    """把单文件转换结果落到 intermediate/。失败时只写日志，不影响主流程。"""
    try:
        cfg.intermediate_dir.mkdir(parents=True, exist_ok=True)
        # 1) 主 markdown
        md_path = cfg.intermediate_dir / _safe_filename(sf.rel_path, sf.file_id)
        meta_lines = [
            f"<!-- intermediate dump for {sf.rel_path} -->",
            f"<!-- file_id={sf.file_id} kind={sf.kind} status={result.status} -->",
            "",
            f"**源文件**: `{sf.rel_path}`",
            f"- 类型: {sf.kind}",
            f"- 大小: {sf.size_bytes:,} bytes",
            f"- 修改时间: {sf.mtime}",
            f"- 创建时间: {sf.ctime} ({sf.ctime_source})",
            f"- 内容哈希: {sf.content_hash or '(未计算)'} "
            f"({sf.content_hash_algo or sf.content_hash_skipped_reason or '-'})",
            f"- 状态: {result.status}",
        ]
        if result.warnings:
            meta_lines.append(f"- 警告: {len(result.warnings)} 条")
            for w in result.warnings:
                meta_lines.append(f"  - {w}")
        if result.error:
            meta_lines.append(f"- 错误: {result.error}")
        if result.skip_reason:
            meta_lines.append(f"- 跳过原因: {result.skip_reason}")
        meta_lines.append("\n---\n")

        body = "\n".join(meta_lines) + "\n" + (result.markdown or "")
        md_path.write_text(body, encoding="utf-8")

        # 2) 附 .extra.json（机器友好）
        extra_path = md_path.with_suffix(".extra.json")
        try:
            extra_path.write_text(
                json.dumps(
                    {
                        "rel_path": str(sf.rel_path),
                        "file_id": sf.file_id,
                        "kind": sf.kind,
                        "size_bytes": sf.size_bytes,
                        "mtime": sf.mtime.isoformat() if sf.mtime else None,
                        "ctime": sf.ctime.isoformat() if sf.ctime else None,
                        "ctime_source": sf.ctime_source,
                        "content_hash": sf.content_hash,
                        "content_hash_algo": sf.content_hash_algo,
                        "content_hash_skipped_reason": sf.content_hash_skipped_reason,
                        "status": result.status,
                        "error": result.error,
                        "skip_reason": result.skip_reason,
                        "warnings": result.warnings,
                        # extra 可能含非 JSON 友好对象（如 list of bytes）；尽量兜底
                        "extra": _jsonable(result.extra),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as e:  # 不致命
            logging.getLogger("convert").debug(f"写 extra.json 失败 {extra_path}: {e}")

    except Exception as e:
        logging.getLogger("convert").warning(
            f"保存中间产物失败 {sf.rel_path}: {e.__class__.__name__}: {e}"
        )


def _spool_file_block(
    cfg: AppConfig, sf: ScannedFile, result: ConversionResult
) -> None:
    """把单文件的完整 file_block 写到 spool 目录,并释放 result.markdown 内存。

    为什么需要这一层：
        174997 个文件场景下,所有 result.markdown 同时驻留内存会 OOM
        (即便 TRUNC 单文件控制在 ~100KB,累计也 ~17GB)。
        改成"立即落盘 + 释放"后,任意时刻内存里只有当前单文件的内容,
        splitter 阶段再流式读取 spool。

    spool 文件命名: spool/{file_id}.block (UTF-8 文本)
    """
    # 局部 import,避免顶部 import 循环 (merger -> splitter -> merger)
    from merger import build_file_block

    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    # 用 file_id 命名,避免依赖 rel_path 里的特殊字符
    spool_path = cfg.spool_dir / f"{sf.file_id}.block"

    # build_file_block 需要 FileEntry,但我们这里没构造,临时塞一个
    from merger import FileEntry as _FE
    block_text = build_file_block(_FE(scanned=sf, result=result), cfg)

    block_bytes = block_text.encode("utf-8")
    spool_path.write_bytes(block_bytes)

    result.block_spool_path = spool_path
    result.block_spool_bytes = len(block_bytes)
    # 释放内存:markdown 已经在 spool 文件里了,后面不会再用
    result.markdown = ""


def _jsonable(obj):
    """尽量把任意 dict/list 转成 JSON 友好结构。"""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    try:
        return str(obj)
    except Exception:
        return "<unrepr>"


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)

    # ---- 提前读一次 .env，用于解析 INPUT_DIR / OUTPUT_DIR / OUTPUT_TIMESTAMP_SUFFIX ----
    # （正式 load_config 还会再读一次，是幂等的）
    import os as _os
    from dotenv import load_dotenv as _ld
    if args.env and args.env.exists():
        _ld(dotenv_path=args.env, override=False)
    else:
        _ld(override=False)
    # 同时加载 .env.default 作为兜底（不覆盖已设值）
    _default_env_path = Path(__file__).resolve().parent / ".env.default"
    if _default_env_path.exists():
        _ld(dotenv_path=_default_env_path, override=False)

    # ---- 解析 input_dir：CLI > .env (INPUT_DIR) ----
    input_dir = args.input
    if input_dir is None:
        env_input = _os.getenv("INPUT_DIR", "").strip()
        if env_input:
            input_dir = Path(env_input)
        else:
            print(
                "[启动错误] 未指定输入目录。请：\n"
                "  1) 在命令行加 --input <路径>，或\n"
                "  2) 在 .env / .env.default 中设置 INPUT_DIR=<路径>",
                file=sys.stderr,
            )
            return 2

    # ---- 解析 output_dir：CLI > .env (OUTPUT_DIR) ----
    output_dir = args.output
    if output_dir is None:
        env_output = _os.getenv("OUTPUT_DIR", "").strip()
        if env_output:
            output_dir = Path(env_output)
        else:
            print(
                "[启动错误] 未指定输出目录。请：\n"
                "  1) 在命令行加 --output <路径>，或\n"
                "  2) 在 .env / .env.default 中设置 OUTPUT_DIR=<路径>",
                file=sys.stderr,
            )
            return 2

    # ---- 处理 --output 时间戳后缀 ----
    # 决策优先级：CLI > .env (OUTPUT_TIMESTAMP_SUFFIX) > 默认 True
    timestamp_enabled = args.timestamp_output
    if timestamp_enabled is None:
        env_val = _os.getenv("OUTPUT_TIMESTAMP_SUFFIX", "").strip().lower()
        if env_val in ("0", "false", "no", "off"):
            timestamp_enabled = False
        elif env_val in ("1", "true", "yes", "on"):
            timestamp_enabled = True
        else:
            timestamp_enabled = True  # 默认开

    if timestamp_enabled:
        import datetime as _dt
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = output_dir.name
        if not name:
            # 极端：用户传了 "./" 之类
            name = "out"
            output_dir = output_dir.parent / name
        original_output_dir = output_dir
        output_dir = output_dir.with_name(f"{name}_{ts}")
    else:
        original_output_dir = output_dir

    # 加载配置
    cfg = load_config(
        input_dir=input_dir,
        output_dir=output_dir,
        cli_max_size_mb=args.max_size_mb,
        cli_verbose=args.verbose,
        cli_no_ocr=args.no_ocr,
        cli_filter_mtime=args.filter_mtime,
        cli_keep_intermediate=args.keep_intermediate,
        cli_file_metadata=args.file_metadata,
        cli_dedup=args.dedup,
        cli_metadata_position=args.metadata_position,
        env_path=args.env,
    )
    _setup_logging(cfg)

    logger = logging.getLogger("convert")
    if args.input is None:
        logger.info(f"输入目录来自 .env：{input_dir}")
    if args.output is None:
        logger.info(f"输出目录来自 .env：{original_output_dir}")
    if timestamp_enabled:
        logger.info(f"输出目录已自动加时间戳后缀：{output_dir}")
    cfg.print_effective()

    # 扫描（三层架构）：
    #   all_files: 全量枚举结果（用于「完整文件目录」、查重等）
    #   eligible_files: 通过所有筛选（ext/size/mtime/ctime）的子集 → 才进入提取流水线
    all_files, eligible_files = scan(cfg)
    if not all_files:
        logger.warning("输入目录中没有任何文件，退出。")
        return 0
    if not eligible_files:
        logger.warning(
            f"枚举到 {len(all_files)} 个文件，但全部被进目录筛选拒绝；"
            "请检查 .env 中 ELIGIBILITY_* / SIZE_FILTER_* / MTIME_* / CTIME_* 配置。"
        )
        # 注意：即便 eligible 为空，全量目录依然需要写出来（用户能看清楚被拒原因）
        # 流水线仍会跑但生成 0 个 entry。
    # 为后续保留旧变量名兼容：scanned_files 指向 eligible 子集
    scanned_files = eligible_files
    filtered_by_mtime = [f for f in all_files if not f.eligible and any(
        r.startswith("mtime_filter") for r in f.eligibility_reasons
    )]
    filtered_by_size = [f for f in all_files if not f.eligible and any(
        r.startswith("size_filter") for r in f.eligibility_reasons
    )]
    filtered_by_ext = [f for f in all_files if not f.eligible and any(
        r.startswith("ext_filter") for r in f.eligibility_reasons
    )]
    filtered_by_ctime = [f for f in all_files if not f.eligible and any(
        r.startswith("ctime_filter") for r in f.eligibility_reasons
    )]

    # 文件内容哈希（按需）— 用于元信息显示和 / 或查重
    if cfg.needs_content_hash:
        from hasher import compute_hashes
        compute_hashes(cfg, scanned_files)

    # 查重（按需）
    dup_report = None
    if cfg.duplicate_detection_enabled:
        from dedup import find_duplicates
        dup_report = find_duplicates(cfg, scanned_files)
        if dup_report.groups:
            logger.warning(
                f"发现 {len(dup_report.groups)} 组重复文件，"
                f"共 {dup_report.total_redundant_files} 个冗余副本"
            )

    # OCR 客户端
    ocr_client: BaiduOCRClient | None = None
    if cfg.ocr_enabled:
        try:
            ocr_client = BaiduOCRClient(
                api_key=cfg.baidu_api_key,
                secret_key=cfg.baidu_secret_key,
                engine=cfg.ocr_engine,
                max_retries=cfg.ocr_max_retries,
                min_interval=cfg.ocr_min_interval,
                token_cache_path=cfg.ocr_token_cache,
                max_image_bytes=cfg.ocr_max_image_bytes,
            )
            logger.info(f"OCR 客户端就绪，引擎: {cfg.ocr_engine}")
        except Exception as e:
            logger.error(f"OCR 客户端初始化失败: {e}")
            logger.error("将退化为 OCR 关闭模式（图片全部跳过）继续运行。")
            ocr_client = None

    converters = _build_converter_map(cfg, ocr_client)

    # 转换
    entries: list[FileEntry] = []
    ocr_stats = OcrStats()

    for sf in tqdm(scanned_files, desc="转换中", unit="file"):
        conv: BaseConverter | None = None
        if sf.kind == "unsupported":
            result = _convert_unsupported(sf)
        else:
            conv = converters.get(sf.kind)
            if conv is None:
                result = _convert_unsupported(sf)
            else:
                try:
                    result = conv.convert(sf.path)
                except Exception as e:
                    logger.exception(f"转换 {sf.rel_path} 时发生未捕获异常")
                    result = ConversionResult(
                        status="failed",
                        markdown="",
                        error=f"未捕获异常 {e.__class__.__name__}: {e}",
                    )

        # OCR 统计
        if sf.kind == "image" and result.status == "success":
            ocr_stats.call_count += 1
            ocr_stats.total_chars += int(result.extra.get("ocr_chars", 0) or 0)
            ocr_stats.total_seconds += float(result.extra.get("ocr_elapsed", 0.0) or 0.0)

        # 统一的 post-convert 截取：每个 converter 不必重复实现；
        # spec 来自 cfg.truncation_catalog（按 ext/kind 查找）。
        if (
            sf.kind != "unsupported"
            and conv is not None
            and result.status == "success"
        ):
            try:
                result = conv._post_convert_truncate(result, path=sf.path)
            except Exception as e:
                logger.warning(
                    f"post-convert 截取异常（{sf.rel_path}）：{e.__class__.__name__}: {e}"
                )

        entries.append(FileEntry(scanned=sf, result=result))

        # 中间产物落盘（无论成功失败都保留）—— 必须在 spool 释放 markdown 之前
        if cfg.keep_intermediate:
            _save_intermediate(cfg, sf, result)

        # 把单文件的完整 file_block 写到 spool/,并释放 result.markdown 内存。
        # 这是 174997+ 文件场景下避免 OOM 的核心：splitter 后续会流式读 spool。
        try:
            _spool_file_block(cfg, sf, result)
        except Exception as e:
            logger.warning(
                f"写 spool 失败（{sf.rel_path}）：{e.__class__.__name__}: {e}；"
                "splitter 会在缺少 spool 时回落到 markdown 内存（可能导致内存升高）"
            )

        if result.status == "failed":
            logger.warning(f"❌ 失败 {sf.rel_path}: {result.error}")
        elif result.status == "skipped":
            logger.info(f"🚫 跳过 {sf.rel_path}: {result.skip_reason}")
        else:
            logger.debug(f"✅ 成功 {sf.rel_path}")

    # 分卷写文件
    logger.info("开始合并 + 分卷 ...")
    volume_paths = split_and_write(
        cfg, entries, dup_report=dup_report, all_files=all_files
    )
    logger.info(f"分卷完成：共 {len(volume_paths)} 卷")

    # 报告
    report_path = cfg.output_dir / "conversion_report.md"
    write_report(
        cfg=cfg,
        entries=entries,
        volume_paths=volume_paths,
        ocr_stats=ocr_stats,
        report_path=report_path,
        filtered_out=filtered_by_mtime,
        filtered_by_size=filtered_by_size,
        filtered_by_ext=filtered_by_ext,
        filtered_by_ctime=filtered_by_ctime,
        all_files=all_files,
        dup_report=dup_report,
    )
    logger.info(f"报告已生成: {report_path}")

    # 🌲 被拒清单 tree 视图（独立文件，便于人工浏览 / 调优 .env）
    try:
        from rejected_tree import render_rejected_tree_markdown
        rejected_tree_md = render_rejected_tree_markdown(
            filtered_by_ext=filtered_by_ext,
            filtered_by_size=filtered_by_size,
            filtered_by_mtime=filtered_by_mtime,
            filtered_by_ctime=filtered_by_ctime,
            entries=entries,
            input_dir_label=str(cfg.input_dir),
        )
        rejected_tree_path = cfg.output_dir / "rejected_tree.md"
        rejected_tree_path.write_text(rejected_tree_md, encoding="utf-8")
        logger.info(f"被拒清单 tree 视图已生成: {rejected_tree_path}")
    except Exception as e:
        logger.warning(
            f"生成 rejected_tree.md 失败（不影响主流程）: {e.__class__.__name__}: {e}"
        )

    # 中间产物索引（如果开启）
    if cfg.keep_intermediate:
        index_path = cfg.intermediate_dir / "INDEX.md"
        _write_intermediate_index(cfg, entries, index_path)
        logger.info(f"中间产物索引已生成: {index_path}")

    # 总览
    n_ok = sum(1 for e in entries if e.result.status == "success")
    n_fail = sum(1 for e in entries if e.result.status == "failed")
    n_skip = sum(1 for e in entries if e.result.status == "skipped")
    logger.info(
        f"完成：枚举 {len(all_files)}, 成功 {n_ok}, 失败 {n_fail}, 跳过 {n_skip}, "
        f"共 {len(volume_paths)} 卷"
        + (f", ext 过滤掉 {len(filtered_by_ext)} 个" if filtered_by_ext else "")
        + (f", size 过滤掉 {len(filtered_by_size)} 个" if filtered_by_size else "")
        + (f", mtime 过滤掉 {len(filtered_by_mtime)} 个" if filtered_by_mtime else "")
        + (f", ctime 过滤掉 {len(filtered_by_ctime)} 个" if filtered_by_ctime else "")
    )
    return 0


def _write_intermediate_index(
    cfg: AppConfig, entries: list[FileEntry], index_path: Path
) -> None:
    """生成 intermediate/INDEX.md，方便人工查阅每个文件对应的中间产物。"""
    lines: list[str] = []
    lines.append("# 🗂️ Intermediate Files Index\n")
    lines.append("")
    lines.append(f"- 共 {len(entries)} 个文件\n")
    lines.append("- 每个文件含两份产物：`*.md`（人类可读）+ `*.extra.json`（机器可读）")
    lines.append("")
    lines.append("| # | 源文件 | 状态 | 类型 | 中间产物 |")
    lines.append("|---|--------|------|------|----------|")
    for i, e in enumerate(entries, start=1):
        fname = _safe_filename(e.scanned.rel_path, e.scanned.file_id)
        rel = str(e.scanned.rel_path).replace("|", "\\|")
        status = {"success": "✅", "failed": "❌", "skipped": "🚫"}.get(
            e.result.status, e.result.status
        )
        lines.append(
            f"| {i} | {rel} | {status} | {e.scanned.kind} | "
            f"[{fname}](./{fname}) / "
            f"[{fname[:-3]}.extra.json](./{fname[:-3]}.extra.json) |"
        )
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
