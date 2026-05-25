"""配置加载模块。

合并优先级：CLI 参数 > .env 文件 > 代码内置默认值。
启动时会打印实际生效的关键配置（敏感字段脱敏）。
"""
from __future__ import annotations

import datetime as _dt
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


def _str_to_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


def _mask_secret(s: str) -> str:
    if not s:
        return "<空>"
    if len(s) <= 4:
        return "***"
    return f"{s[:4]}***"


def _parse_date(value: str, *, field_name: str) -> _dt.datetime | None:
    """把 .env 里的日期字符串解析为 datetime。

    支持 YYYY-MM-DD 与 YYYY-MM-DD HH:MM:SS 两种格式；空串/None 返回 None。
    """
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return _dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise SystemExit(
        f"[配置错误] {field_name}={value} 无法解析为日期。"
        "请使用 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS 格式。"
    )


@dataclass
class AppConfig:
    """应用全局配置对象。在 CLI 入口构造一次，全程传递。"""

    # ---- CLI / 流水线 ----
    input_dir: Path
    output_dir: Path
    max_size_mb: int = 19
    verbose: bool = False
    no_ocr_cli: bool = False  # CLI --no-ocr 标记，会强制覆盖 ocr_enabled

    # ---- 通用参数（来自 .env） ----
    log_level: str = "INFO"
    csv_max_rows: int = 200
    scanned_pdf_char_per_page: int = 50

    # ---- OCR 参数 ----
    ocr_enabled: bool = False
    baidu_api_key: str = ""
    baidu_secret_key: str = ""
    ocr_engine: str = "accurate_basic"
    ocr_max_retries: int = 3
    ocr_min_interval: float = 0.5
    ocr_token_cache: Path = field(default_factory=lambda: Path(".baidu_token_cache.json"))
    ocr_image_exts: tuple[str, ...] = (
        "png", "jpg", "jpeg", "gif", "webp", "bmp", "tiff",
    )
    ocr_max_image_bytes: int = 4 * 1024 * 1024

    # ---- 修改日期筛选（可选）----
    mtime_filter_enabled: bool = False
    mtime_after: _dt.datetime | None = None   # 包含
    mtime_before: _dt.datetime | None = None  # 包含

    # ---- 创建日期筛选（可选；语义见 scanner.ctime_source）----
    ctime_filter_enabled: bool = False
    ctime_after: _dt.datetime | None = None
    ctime_before: _dt.datetime | None = None

    # ---- 进目录扩展名 / kind 筛选（资格层）----
    # 在 load_config() 中填充；None 表示「不筛选，全部放行」
    eligibility_ext_filter: object = None  # 实际类型：ext_filter.ExtensionFilter

    # ---- 枚举层物理跳过的「垃圾」清单（除默认外的用户扩展）----
    # 默认清单见 scanner.DEFAULT_PURGE_DIR_NAMES / PURGE_FILENAMES / PURGE_SUFFIXES
    purge_dir_names_extra: set[str] = field(default_factory=set)
    purge_filenames_extra: set[str] = field(default_factory=set)
    purge_suffixes_extra: set[str] = field(default_factory=set)

    # ---- 完整文件目录（全量枚举清单）----
    # 该清单不受任何筛选影响，由 merger 在合并 MD 顶部输出，由 reporter 在报告中输出。
    full_inventory_in_merged: bool = True   # 是否把全量清单写进每卷 merged_part_*.md 顶部
    full_inventory_in_report: bool = True   # 是否在 conversion_report.md 写一份

    # ---- 日志类文件单文件截取（仅 .log .tlog .rlog 受影响）----
    log_file_max_bytes: int = 10 * 1024 * 1024  # 10 MB
    log_truncate_head_ratio: float = 0.5         # 截断时头部占比，剩下给尾部

    # ---- 中间过程文件保留 ----
    keep_intermediate: bool = False
    intermediate_subdir: str = "intermediate"

    # ---- 文件元信息附注（可选）----
    # 启用后会计算每个文件的内容哈希；元信息(哈希/修改日期/创建日期/大小)按
    # file_metadata_position 写到 TOC、正文文件头、或两处。
    file_metadata_enabled: bool = False
    file_hash_algo: str = "sha256"          # sha256 / sha1 / md5
    file_metadata_position: str = "body"     # toc / body / both / none
    file_metadata_show_hash: bool = True
    file_metadata_show_mtime: bool = True
    file_metadata_show_ctime: bool = True
    file_metadata_show_size: bool = True
    file_hash_max_bytes: int = 0             # 0 表示不限；>0 时大于该值的文件跳过哈希
    file_hash_short_len: int = 16            # TOC 紧凑显示时的哈希前缀长度

    # ---- 重复文件检测（可选）----
    # 启用此项会自动启用 file_metadata_enabled 与哈希计算
    duplicate_detection_enabled: bool = False

    # ---- 通用文件截取（覆盖所有文件类型，按扩展名/kind 分别配置）----
    # 老的 LOG_FILE_MAX_BYTES / LOG_TRUNCATE_HEAD_RATIO 仍生效（仅对 log/tlog/rlog 的 post-convert
    # 兜底），但日常使用建议改用新的 TRUNC_* 体系，颗粒度更细。
    truncation_enabled: bool = True   # 总开关：false 时所有类型都不截
    truncation_seed: int = 0
    # 在 load_config() 中填充：每个 kind / ext 对应的 TruncationSpec
    truncation_catalog: object = None  # 实际类型：truncation.TruncationCatalog；为避免循环 import 用 object 占位

    # ---- 按文件类型的大小筛选 ----
    # 任一阈值 -1 或 false → 关闭。配置查找优先级：扩展名 > kind > default
    size_filter_enabled: bool = True
    size_filter_catalog: object = None  # 实际类型：size_filter.SizeFilterCatalog

    # 计算属性
    @property
    def max_size_bytes(self) -> int:
        return self.max_size_mb * 1024 * 1024

    @property
    def image_exts_with_dot(self) -> set[str]:
        """返回带点的小写扩展名集合，例如 {'.png', '.jpg'}。"""
        return {f".{e.strip().lower().lstrip('.')}" for e in self.ocr_image_exts if e.strip()}

    @property
    def intermediate_dir(self) -> Path:
        return self.output_dir / self.intermediate_subdir

    @property
    def spool_dir(self) -> Path:
        """单文件 file_block 的临时存放目录。主流程写,splitter 流式读。

        作用:避免所有 file_block 同时驻留内存导致 OOM。
        位置:固定 <output>/.spool/,跑完会被 splitter 清理。
        """
        return self.output_dir / ".spool"

    def validate(self) -> None:
        """启动时自检：OCR 启用但缺 key → 报错退出；日期范围/截断比例无效 → 报错退出。"""
        if self.ocr_enabled and (not self.baidu_api_key or not self.baidu_secret_key):
            raise SystemExit(
                "[配置错误] OCR_ENABLED=true 但 BAIDU_OCR_API_KEY 或 "
                "BAIDU_OCR_SECRET_KEY 未配置。请在 .env 中填入凭据，"
                "或将 OCR_ENABLED 设为 false。"
            )
        if self.ocr_engine not in ("accurate_basic", "general_basic"):
            raise SystemExit(
                f"[配置错误] OCR_ENGINE={self.ocr_engine} 非法，"
                "仅支持 accurate_basic / general_basic。"
            )
        if (
            self.mtime_filter_enabled
            and self.mtime_after is not None
            and self.mtime_before is not None
            and self.mtime_after > self.mtime_before
        ):
            raise SystemExit(
                f"[配置错误] MTIME_AFTER={self.mtime_after} 晚于 "
                f"MTIME_BEFORE={self.mtime_before}，请检查 .env。"
            )
        if (
            self.ctime_filter_enabled
            and self.ctime_after is not None
            and self.ctime_before is not None
            and self.ctime_after > self.ctime_before
        ):
            raise SystemExit(
                f"[配置错误] CTIME_AFTER={self.ctime_after} 晚于 "
                f"CTIME_BEFORE={self.ctime_before}，请检查 .env。"
            )
        if self.log_file_max_bytes <= 0:
            raise SystemExit(
                f"[配置错误] LOG_FILE_MAX_BYTES={self.log_file_max_bytes} 必须 > 0。"
            )
        if not (0.0 < self.log_truncate_head_ratio < 1.0):
            raise SystemExit(
                f"[配置错误] LOG_TRUNCATE_HEAD_RATIO={self.log_truncate_head_ratio} "
                "必须在 (0, 1) 区间内。"
            )
        # 文件元信息 / 哈希校验
        if self.file_metadata_position not in ("toc", "body", "both", "none"):
            raise SystemExit(
                f"[配置错误] FILE_METADATA_POSITION={self.file_metadata_position} 非法，"
                "仅支持 toc / body / both / none。"
            )
        if self.file_hash_algo not in ("sha256", "sha1", "md5"):
            raise SystemExit(
                f"[配置错误] FILE_HASH_ALGO={self.file_hash_algo} 非法，"
                "仅支持 sha256 / sha1 / md5。"
            )
        if self.file_hash_max_bytes < 0:
            raise SystemExit(
                f"[配置错误] FILE_HASH_MAX_BYTES={self.file_hash_max_bytes} 不能为负。"
            )
        if self.file_hash_short_len < 4 or self.file_hash_short_len > 64:
            raise SystemExit(
                f"[配置错误] FILE_HASH_SHORT_LEN={self.file_hash_short_len} 必须在 [4, 64]。"
            )
        # 启用查重 → 自动开元信息（用户可不显式设置 metadata_enabled）
        if self.duplicate_detection_enabled and not self.file_metadata_enabled:
            self.file_metadata_enabled = True
            # 不强行覆盖 show_hash，但查重必须算 hash —— 这是 hasher 的行为，
            # 与"是否在 TOC/正文中显示哈希"无关。

    @property
    def needs_content_hash(self) -> bool:
        """是否需要计算文件内容哈希。

        条件：开了查重，或者开了元信息且要显示哈希。
        位置 = none 时即使开了 metadata 也用不上 hash 来显示，但若同时开了查重仍要算。
        """
        if self.duplicate_detection_enabled:
            return True
        if not self.file_metadata_enabled:
            return False
        if self.file_metadata_position == "none":
            return False
        return self.file_metadata_show_hash

    def print_effective(self) -> None:
        """打印生效配置（脱敏）。"""
        logger.info("========== 生效配置 ==========")
        logger.info(f"  input_dir              = {self.input_dir}")
        logger.info(f"  output_dir             = {self.output_dir}")
        logger.info(f"  max_size_mb            = {self.max_size_mb}")
        logger.info(f"  verbose                = {self.verbose}")
        logger.info(f"  log_level              = {self.log_level}")
        logger.info(f"  csv_max_rows           = {self.csv_max_rows}")
        logger.info(f"  scanned_pdf_char/page  = {self.scanned_pdf_char_per_page}")
        logger.info(f"  ocr_enabled            = {self.ocr_enabled}"
                    + ("  (CLI --no-ocr 强制关闭)" if self.no_ocr_cli else ""))
        logger.info(f"  ocr_engine             = {self.ocr_engine}")
        logger.info(f"  baidu_api_key          = {_mask_secret(self.baidu_api_key)}")
        logger.info(f"  baidu_secret_key       = {_mask_secret(self.baidu_secret_key)}")
        logger.info(f"  ocr_max_retries        = {self.ocr_max_retries}")
        logger.info(f"  ocr_min_interval       = {self.ocr_min_interval}s")
        logger.info(f"  ocr_token_cache        = {self.ocr_token_cache}")
        logger.info(f"  ocr_image_exts         = {','.join(self.ocr_image_exts)}")
        logger.info(f"  ocr_max_image_bytes    = {self.ocr_max_image_bytes} bytes")
        logger.info(f"  mtime_filter_enabled   = {self.mtime_filter_enabled}")
        if self.mtime_filter_enabled:
            logger.info(f"  mtime_after            = {self.mtime_after or '(不限)'}")
            logger.info(f"  mtime_before           = {self.mtime_before or '(不限)'}")
        logger.info(f"  log_file_max_bytes     = {self.log_file_max_bytes:,} bytes "
                    f"(影响 .log .tlog .rlog)")
        logger.info(f"  log_truncate_head_ratio= {self.log_truncate_head_ratio}")
        logger.info(f"  keep_intermediate      = {self.keep_intermediate}")
        if self.keep_intermediate:
            logger.info(f"  intermediate_dir       = {self.intermediate_dir}")
        logger.info(f"  file_metadata_enabled  = {self.file_metadata_enabled}")
        if self.file_metadata_enabled:
            logger.info(f"  file_hash_algo         = {self.file_hash_algo}")
            logger.info(f"  file_metadata_position = {self.file_metadata_position}")
            logger.info(
                f"  metadata_show          = hash:{self.file_metadata_show_hash}, "
                f"mtime:{self.file_metadata_show_mtime}, "
                f"ctime:{self.file_metadata_show_ctime}, "
                f"size:{self.file_metadata_show_size}"
            )
            if self.file_hash_max_bytes > 0:
                logger.info(
                    f"  file_hash_max_bytes    = {self.file_hash_max_bytes:,} "
                    "（超过此值的文件不算哈希）"
                )
            else:
                logger.info("  file_hash_max_bytes    = 0（不限）")
            logger.info(f"  file_hash_short_len    = {self.file_hash_short_len}")
        logger.info(f"  duplicate_detection    = {self.duplicate_detection_enabled}")
        # 新：通用文件截取
        logger.info(f"  truncation_enabled     = {self.truncation_enabled}")
        if self.truncation_enabled and self.truncation_catalog is not None:
            logger.info(f"  truncation_seed        = {self.truncation_seed}")
            try:
                for line in self.truncation_catalog.known_keys_describe():
                    logger.info(line)
            except Exception:
                pass
        # 新：按文件类型的大小筛选
        logger.info(f"  size_filter_enabled    = {self.size_filter_enabled}")
        if self.size_filter_enabled and self.size_filter_catalog is not None:
            try:
                for line in self.size_filter_catalog.known_keys_describe():
                    logger.info(line)
            except Exception:
                pass
        logger.info("===============================")


def load_config(
    *,
    input_dir: Path,
    output_dir: Path,
    cli_max_size_mb: int | None,
    cli_verbose: bool,
    cli_no_ocr: bool,
    cli_filter_mtime: bool | None = None,
    cli_keep_intermediate: bool | None = None,
    cli_file_metadata: bool | None = None,
    cli_dedup: bool | None = None,
    cli_metadata_position: str | None = None,
    env_path: Path | None = None,
) -> AppConfig:
    """加载 .env 并合并 CLI 参数，返回 AppConfig。

    Args:
        input_dir: --input
        output_dir: --output
        cli_max_size_mb: --max-size-mb（None 表示 CLI 未指定，用 .env / 默认值）
        cli_verbose: --verbose
        cli_no_ocr: --no-ocr
        cli_filter_mtime: --filter-mtime / --no-filter-mtime（None 表示用 .env）
        cli_keep_intermediate: --keep-intermediate / --no-keep-intermediate（None 表示用 .env）
        cli_file_metadata: --with-metadata / --no-metadata（None 表示用 .env）
        cli_dedup: --dedup / --no-dedup（None 表示用 .env）
        cli_metadata_position: --metadata-position（None 表示用 .env）
        env_path: .env 文件路径，None 表示用默认搜索逻辑
    """
    # 先加载用户的 .env（如存在），它的设置优先；
    # 再加载仓库内 .env.default（兜底，不覆盖已有变量）。
    if env_path and env_path.exists():
        load_dotenv(dotenv_path=env_path, override=False)
    else:
        load_dotenv(override=False)

    # .env.default：在脚本同目录查找，作为代码内置默认值的来源
    _default_env_path = Path(__file__).resolve().parent / ".env.default"
    if _default_env_path.exists():
        load_dotenv(dotenv_path=_default_env_path, override=False)
    else:
        logger.warning(
            f"未找到默认配置文件 .env.default（预期路径：{_default_env_path}），"
            "将依赖代码内的硬编码默认值"
        )

    def env(key: str, default: str = "") -> str:
        return os.getenv(key, default).strip()

    # --- 基础 ---
    env_max_size = env("MAX_SIZE_MB", "19")
    try:
        env_max_size_int = int(env_max_size)
    except ValueError:
        env_max_size_int = 19

    log_level = env("LOG_LEVEL", "INFO").upper()
    csv_max_rows = int(env("CSV_MAX_ROWS", "200") or 200)
    scanned_pdf_char_per_page = int(env("SCANNED_PDF_CHAR_PER_PAGE", "50") or 50)

    # --- OCR ---
    ocr_enabled = _str_to_bool(env("OCR_ENABLED", "false"), default=False)
    if cli_no_ocr:
        ocr_enabled = False

    baidu_api_key = env("BAIDU_OCR_API_KEY", "")
    baidu_secret_key = env("BAIDU_OCR_SECRET_KEY", "")
    ocr_engine = env("OCR_ENGINE", "accurate_basic") or "accurate_basic"
    ocr_max_retries = int(env("OCR_MAX_RETRIES", "3") or 3)
    ocr_min_interval = float(env("OCR_MIN_INTERVAL", "0.5") or 0.5)
    ocr_token_cache_str = env("OCR_TOKEN_CACHE", ".baidu_token_cache.json") or ".baidu_token_cache.json"
    ocr_token_cache = Path(ocr_token_cache_str)
    exts_raw = env("OCR_IMAGE_EXTS", "png,jpg,jpeg,gif,webp,bmp,tiff")
    ocr_image_exts = tuple(
        e.strip().lower().lstrip(".") for e in exts_raw.split(",") if e.strip()
    )
    ocr_max_image_bytes = int(env("OCR_MAX_IMAGE_BYTES", str(4 * 1024 * 1024)) or 4 * 1024 * 1024)

    # --- 修改日期筛选 ---
    mtime_filter_enabled = _str_to_bool(env("MTIME_FILTER_ENABLED", "false"), default=False)
    if cli_filter_mtime is not None:
        mtime_filter_enabled = cli_filter_mtime
    mtime_after = _parse_date(env("MTIME_AFTER", ""), field_name="MTIME_AFTER")
    mtime_before = _parse_date(env("MTIME_BEFORE", ""), field_name="MTIME_BEFORE")

    # --- 创建日期筛选 ---
    ctime_filter_enabled = _str_to_bool(env("CTIME_FILTER_ENABLED", "false"), default=False)
    ctime_after = _parse_date(env("CTIME_AFTER", ""), field_name="CTIME_AFTER")
    ctime_before = _parse_date(env("CTIME_BEFORE", ""), field_name="CTIME_BEFORE")

    # --- 进目录扩展名 / kind 筛选 ---
    from ext_filter import ExtensionFilter
    def _parse_set(raw: str) -> set[str]:
        return {
            (e.strip().lower() if e.strip().startswith(".") or "/" not in e else e.strip())
            for e in raw.split(",")
            if e.strip()
        }
    include_exts_raw = env("ELIGIBILITY_INCLUDE_EXTS", "")
    include_kinds_raw = env("ELIGIBILITY_INCLUDE_KINDS", "")
    exclude_exts_raw = env("ELIGIBILITY_EXCLUDE_EXTS", "")
    exclude_kinds_raw = env("ELIGIBILITY_EXCLUDE_KINDS", "")
    unsupported_policy = (env("ELIGIBILITY_UNSUPPORTED_POLICY", "allow") or "allow").lower()
    if unsupported_policy not in ("allow", "reject"):
        unsupported_policy = "allow"
    eligibility_ext_filter = ExtensionFilter(
        include_exts={e if e.startswith(".") else "." + e for e in _parse_set(include_exts_raw)},
        include_kinds=_parse_set(include_kinds_raw),
        exclude_exts={e if e.startswith(".") else "." + e for e in _parse_set(exclude_exts_raw)},
        exclude_kinds=_parse_set(exclude_kinds_raw),
        unsupported_policy=unsupported_policy,
    )

    # --- 完整文件目录（全量枚举）开关 ---
    full_inventory_in_merged = _str_to_bool(
        env("FULL_INVENTORY_IN_MERGED", "true"), default=True
    )
    full_inventory_in_report = _str_to_bool(
        env("FULL_INVENTORY_IN_REPORT", "true"), default=True
    )

    # --- 枚举层物理跳过的「垃圾」清单（用户在 .env 中追加）---
    def _split_csv_set(raw: str) -> set[str]:
        return {p.strip() for p in raw.split(",") if p.strip()}

    purge_dir_names_extra = _split_csv_set(env("PURGE_DIR_NAMES_EXTRA", ""))
    purge_filenames_extra = _split_csv_set(env("PURGE_FILENAMES_EXTRA", ""))
    # 后缀归一化：自动加点 + 小写
    raw_suffixes = _split_csv_set(env("PURGE_SUFFIXES_EXTRA", ""))
    purge_suffixes_extra = {
        (s.lower() if s.startswith(".") else "." + s.lower()) for s in raw_suffixes
    }

    # --- 日志文件截取 ---
    log_file_max_bytes = int(
        env("LOG_FILE_MAX_BYTES", str(10 * 1024 * 1024)) or (10 * 1024 * 1024)
    )
    log_truncate_head_ratio = float(env("LOG_TRUNCATE_HEAD_RATIO", "0.5") or 0.5)

    # --- 中间产物保留 ---
    keep_intermediate = _str_to_bool(env("KEEP_INTERMEDIATE_FILES", "false"), default=False)
    if cli_keep_intermediate is not None:
        keep_intermediate = cli_keep_intermediate
    intermediate_subdir = env("INTERMEDIATE_SUBDIR", "intermediate") or "intermediate"

    # --- 文件元信息 / 哈希 ---
    file_metadata_enabled = _str_to_bool(env("FILE_METADATA_ENABLED", "false"), default=False)
    if cli_file_metadata is not None:
        file_metadata_enabled = cli_file_metadata

    file_hash_algo = (env("FILE_HASH_ALGO", "sha256") or "sha256").lower()

    metadata_position = (env("FILE_METADATA_POSITION", "body") or "body").lower()
    if cli_metadata_position:
        metadata_position = cli_metadata_position.lower()

    metadata_show_hash = _str_to_bool(env("FILE_METADATA_SHOW_HASH", "true"), default=True)
    metadata_show_mtime = _str_to_bool(env("FILE_METADATA_SHOW_MTIME", "true"), default=True)
    metadata_show_ctime = _str_to_bool(env("FILE_METADATA_SHOW_CTIME", "true"), default=True)
    metadata_show_size = _str_to_bool(env("FILE_METADATA_SHOW_SIZE", "true"), default=True)

    file_hash_max_bytes = int(env("FILE_HASH_MAX_BYTES", "0") or 0)
    file_hash_short_len = int(env("FILE_HASH_SHORT_LEN", "16") or 16)

    # --- 重复文件检测 ---
    duplicate_detection_enabled = _str_to_bool(
        env("DUPLICATE_DETECTION_ENABLED", "false"), default=False
    )
    if cli_dedup is not None:
        duplicate_detection_enabled = cli_dedup

    # 用户语义：显式 --no-metadata 表示"我不想看到元信息"。如果同时开了 dedup，
    # 后续 validate() 会强行把 file_metadata_enabled 拉回 True 以便算 hash 给查重用。
    # 这种情况下我们把显示位置静默改成 "none"，让 hash 仅用于查重而不在 TOC/正文显示。
    if cli_file_metadata is False and duplicate_detection_enabled and not cli_metadata_position:
        metadata_position = "none"

    # --- 通用文件截取（TRUNC_*）+ 文件大小筛选（SIZE_FILTER_*）---
    # 所有默认值都从 .env.default 通过环境变量读取（已在前面 load_dotenv 加载）；
    # 用户的 .env 优先覆盖。
    from truncation import TruncationCatalog, TruncationSpec
    from size_filter import SizeFilterCatalog, SizeFilterSpec

    def _f(val: str, default: float) -> float:
        try:
            return float(val) if val != "" else default
        except ValueError:
            return default

    def _i(val: str, default: int) -> int:
        try:
            return int(val) if val != "" else default
        except ValueError:
            return default

    # 完整的内部 kind 列表，确保每个 kind 都有 spec（scanner._EXT_TO_KIND 的值集合 ∪ image）
    _ALL_KINDS = (
        "pdf", "docx", "txt", "csv", "xlsx", "json", "markdown", "html",
        "image", "param", "log", "rlog", "tlog", "wps",
    )

    # ---- 截取（TRUNC_*）----
    def _read_trunc_spec(prefix: str, fallback: TruncationSpec) -> TruncationSpec:
        return TruncationSpec(
            enabled=_str_to_bool(env(f"{prefix}_ENABLED", ""), default=fallback.enabled),
            head_mb=_f(env(f"{prefix}_HEAD_MB", ""), fallback.head_mb),
            tail_mb=_f(env(f"{prefix}_TAIL_MB", ""), fallback.tail_mb),
            chunk_mb=_f(env(f"{prefix}_CHUNK_MB", ""), fallback.chunk_mb),
            middle_chunks=_i(env(f"{prefix}_MIDDLE_CHUNKS", ""), fallback.middle_chunks),
        )

    trunc_enabled = _str_to_bool(env("TRUNC_ENABLED", "true"), default=True)
    trunc_seed = _i(env("TRUNC_SEED", "0"), 0)

    # 硬编码兜底：万一 .env.default 缺失，这里保证程序不崩
    _trunc_hard_default = TruncationSpec(
        enabled=False, head_mb=2.0, tail_mb=2.0, chunk_mb=1.0, middle_chunks=2,
    )
    default_trunc_spec = _read_trunc_spec("TRUNC_DEFAULT", _trunc_hard_default)

    by_kind_trunc: dict[str, TruncationSpec] = {}
    for kind in _ALL_KINDS:
        prefix = f"TRUNC_KIND_{kind.upper()}"
        by_kind_trunc[kind] = _read_trunc_spec(prefix, default_trunc_spec)

    # 扫描所有 TRUNC_EXT_<XYZ>_* 环境变量，发现哪些扩展名被显式配置
    seen_ext_keys_trunc: set[str] = set()
    for env_key in os.environ.keys():
        if env_key.startswith("TRUNC_EXT_"):
            tail = env_key[len("TRUNC_EXT_"):]
            for suffix in ("_ENABLED", "_HEAD_MB", "_TAIL_MB", "_CHUNK_MB", "_MIDDLE_CHUNKS"):
                if tail.endswith(suffix):
                    ext_part = tail[: -len(suffix)]
                    if ext_part:
                        seen_ext_keys_trunc.add(ext_part)
                    break
    by_ext_trunc: dict[str, TruncationSpec] = {}
    for ext_part in seen_ext_keys_trunc:
        norm_ext = "." + ext_part.lower()
        prefix = f"TRUNC_EXT_{ext_part}"
        by_ext_trunc[norm_ext] = _read_trunc_spec(prefix, default_trunc_spec)

    trunc_catalog = TruncationCatalog(
        by_ext=by_ext_trunc,
        by_kind=by_kind_trunc,
        default_spec=default_trunc_spec,
        global_seed=trunc_seed,
    )
    if not trunc_enabled:
        for sp in list(trunc_catalog.by_ext.values()) + list(trunc_catalog.by_kind.values()) + [trunc_catalog.default_spec]:
            sp.enabled = False

    # ---- 大小筛选（SIZE_FILTER_*）----
    # 任何值为 -1 或 false 都解释为「关闭该项筛选」
    def _read_size_val(raw: str, default: float) -> float:
        s = raw.strip().lower()
        if s in ("", "-1", "false", "off", "no", "none", "null"):
            return -1.0
        try:
            return float(s)
        except ValueError:
            return default

    def _read_size_spec(prefix: str, fallback: SizeFilterSpec) -> SizeFilterSpec:
        return SizeFilterSpec(
            min_mb=_read_size_val(env(f"{prefix}_MIN_MB", ""), fallback.min_mb),
            max_mb=_read_size_val(env(f"{prefix}_MAX_MB", ""), fallback.max_mb),
        )

    size_filter_enabled = _str_to_bool(env("SIZE_FILTER_ENABLED", "true"), default=True)
    _size_hard_default = SizeFilterSpec(min_mb=-1.0, max_mb=-1.0)  # 都不限
    default_size_spec = _read_size_spec("SIZE_FILTER_DEFAULT", _size_hard_default)

    by_kind_size: dict[str, SizeFilterSpec] = {}
    for kind in _ALL_KINDS:
        prefix = f"SIZE_FILTER_KIND_{kind.upper()}"
        by_kind_size[kind] = _read_size_spec(prefix, default_size_spec)

    seen_ext_keys_size: set[str] = set()
    for env_key in os.environ.keys():
        if env_key.startswith("SIZE_FILTER_EXT_"):
            tail = env_key[len("SIZE_FILTER_EXT_"):]
            for suffix in ("_MIN_MB", "_MAX_MB"):
                if tail.endswith(suffix):
                    ext_part = tail[: -len(suffix)]
                    if ext_part:
                        seen_ext_keys_size.add(ext_part)
                    break
    by_ext_size: dict[str, SizeFilterSpec] = {}
    for ext_part in seen_ext_keys_size:
        norm_ext = "." + ext_part.lower()
        prefix = f"SIZE_FILTER_EXT_{ext_part}"
        by_ext_size[norm_ext] = _read_size_spec(prefix, default_size_spec)

    size_filter_catalog = SizeFilterCatalog(
        by_ext=by_ext_size,
        by_kind=by_kind_size,
        default_spec=default_size_spec,
        enabled=size_filter_enabled,
    )

    # --- 合并 CLI 优先 ---
    final_max_size = cli_max_size_mb if cli_max_size_mb is not None else env_max_size_int

    cfg = AppConfig(
        input_dir=input_dir.resolve(),
        output_dir=output_dir.resolve(),
        max_size_mb=final_max_size,
        verbose=cli_verbose,
        no_ocr_cli=cli_no_ocr,
        log_level=log_level,
        csv_max_rows=csv_max_rows,
        scanned_pdf_char_per_page=scanned_pdf_char_per_page,
        ocr_enabled=ocr_enabled,
        baidu_api_key=baidu_api_key,
        baidu_secret_key=baidu_secret_key,
        ocr_engine=ocr_engine,
        ocr_max_retries=ocr_max_retries,
        ocr_min_interval=ocr_min_interval,
        ocr_token_cache=ocr_token_cache,
        ocr_image_exts=ocr_image_exts,
        ocr_max_image_bytes=ocr_max_image_bytes,
        mtime_filter_enabled=mtime_filter_enabled,
        mtime_after=mtime_after,
        mtime_before=mtime_before,
        log_file_max_bytes=log_file_max_bytes,
        log_truncate_head_ratio=log_truncate_head_ratio,
        keep_intermediate=keep_intermediate,
        intermediate_subdir=intermediate_subdir,
        file_metadata_enabled=file_metadata_enabled,
        file_hash_algo=file_hash_algo,
        file_metadata_position=metadata_position,
        file_metadata_show_hash=metadata_show_hash,
        file_metadata_show_mtime=metadata_show_mtime,
        file_metadata_show_ctime=metadata_show_ctime,
        file_metadata_show_size=metadata_show_size,
        file_hash_max_bytes=file_hash_max_bytes,
        file_hash_short_len=file_hash_short_len,
        duplicate_detection_enabled=duplicate_detection_enabled,
        truncation_enabled=trunc_enabled,
        truncation_seed=trunc_seed,
        truncation_catalog=trunc_catalog,
        size_filter_enabled=size_filter_enabled,
        size_filter_catalog=size_filter_catalog,
        ctime_filter_enabled=ctime_filter_enabled,
        ctime_after=ctime_after,
        ctime_before=ctime_before,
        eligibility_ext_filter=eligibility_ext_filter,
        full_inventory_in_merged=full_inventory_in_merged,
        full_inventory_in_report=full_inventory_in_report,
        purge_dir_names_extra=purge_dir_names_extra,
        purge_filenames_extra=purge_filenames_extra,
        purge_suffixes_extra=purge_suffixes_extra,
    )
    cfg.validate()
    return cfg
