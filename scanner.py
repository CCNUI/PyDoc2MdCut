"""文件扫描模块：递归扫描输入目录，识别文件类型，按需做修改时间过滤。"""
from __future__ import annotations

import datetime as _dt
import hashlib
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

from config import AppConfig

logger = logging.getLogger(__name__)


# 主类型映射（小写带点扩展名 → 内部类型标识）
_EXT_TO_KIND: dict[str, str] = {
    # 文档
    ".pdf": "pdf",
    ".docx": "docx",
    ".txt": "txt",
    ".csv": "csv",
    ".xlsx": "xlsx",
    ".json": "json",
    ".md": "markdown",
    ".markdown": "markdown",
    ".html": "html",
    ".htm": "html",
    # —— 旧版 Office (OLE 复合二进制) ——
    # 仅走 olefile 兜底:抽可打印字串,格式信息丢失。
    # 完整渲染需要装 LibreOffice (参 wps_converter)。
    ".doc": "doc",
    ".dot": "doc",
    ".xls": "xls",
    ".xlt": "xls",
    ".ppt": "ppt",
    ".pot": "ppt",
    ".pps": "ppt",
    # —— 新增 ——
    ".param": "param",       # MissionPlanner 参数备份
    ".log": "log",           # 通用日志（受 LOG_FILE_MAX_BYTES 截取）
    ".rlog": "rlog",         # ROS / ArduPilot 原始日志（文本或二进制，自动判断）
    ".tlog": "tlog",         # MAVLink 遥测日志（二进制）
    ".wps": "wps",           # 金山 WPS 文档
    # —— 代码 / 配置类（按文本处理，用对应语言代码块包裹）——
    ".py": "code_py",
    ".pyw": "code_py",
    ".sh": "code_sh",
    ".bash": "code_sh",
    ".js": "code_js",
    ".ts": "code_ts",
    ".jsx": "code_js",
    ".tsx": "code_ts",
    ".java": "code_java",
    ".c": "code_c",
    ".h": "code_c",
    ".cpp": "code_cpp",
    ".hpp": "code_cpp",
    ".cc": "code_cpp",
    ".cs": "code_cs",
    ".go": "code_go",
    ".rs": "code_rs",
    ".rb": "code_rb",
    ".php": "code_php",
    ".swift": "code_swift",
    ".kt": "code_kt",
    ".scala": "code_scala",
    ".m": "code_m",
    ".mm": "code_mm",
    ".r": "code_r",
    ".sql": "code_sql",
    ".yaml": "code_yaml",
    ".yml": "code_yaml",
    ".toml": "code_toml",
    ".ini": "code_ini",
    ".cfg": "code_ini",
    ".conf": "code_ini",
    ".xml": "code_xml",
    ".css": "code_css",
    ".scss": "code_css",
    ".less": "code_css",
    ".dockerfile": "code_dockerfile",
    # —— Windows PE 可执行文件 / 库 ——
    ".exe": "exe",
    ".dll": "exe",
    ".sys": "exe",
    ".ocx": "exe",
    ".cpl": "exe",
    ".scr": "exe",
    # —— 压缩包 ——
    ".zip": "archive_zip",
    ".jar": "archive_zip",   # JAR 本质是 zip
    ".war": "archive_zip",
    ".apk": "archive_zip",
    ".rar": "archive_rar",
    ".7z": "archive_7z",
    ".tar": "archive_tar",
    ".tgz": "archive_tar",
    ".tbz2": "archive_tar",
    ".txz": "archive_tar",
    ".gz": "archive_gz",     # 注意：.tar.gz 双扩展名由 Path.suffix 只取 .gz；
                              #       archive converter 内部用文件名判别走 tar 分支
    ".bz2": "archive_gz",
    ".xz": "archive_gz",
}


# 「枚举层物理跳过」清单（不属于用户业务筛选；这些都是开发/系统垃圾，跳过它们
# 的语义和「跳过 .env / .DS_Store」一致——不应进入「全量目录」也不应进入提取）。
# 可被 .env 的 PURGE_DIR_NAMES / PURGE_FILE_GLOBS 扩展。
DEFAULT_PURGE_DIR_NAMES = frozenset({
    # Python
    "__pycache__", ".venv", "venv", "env", ".env-venv",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox",
    ".ipynb_checkpoints",
    # Node / JS
    "node_modules", ".next", ".nuxt", ".turbo", ".parcel-cache",
    # 构建产物
    "build", "dist", "out", "target", "bin", "obj",
    ".gradle", ".maven",
    # IDE / 编辑器
    ".idea", ".vscode", ".vs", ".history",
    # 版本控制
    ".git", ".svn", ".hg",
    # OS / 系统
    "$RECYCLE.BIN", "System Volume Information", ".Trash", ".Trashes",
    "$Recycle.Bin",
    # 临时
    "Temp", "temp", "tmp", ".cache",
})

DEFAULT_PURGE_FILENAMES = frozenset({
    ".DS_Store", "Thumbs.db", "desktop.ini", "ehthumbs.db",
    ".gitignore", ".gitkeep", ".gitattributes",
})

# 以这些后缀结尾的临时/缓存文件被「物理跳过」
DEFAULT_PURGE_SUFFIXES = frozenset({
    ".pyc", ".pyo", ".class", ".o", ".obj",
    ".swp", ".swo",  # vim 临时
    ".lock",         # 锁文件
})


@dataclass
class ScannedFile:
    """扫描到的单个文件描述。"""

    path: Path                # 绝对路径
    rel_path: Path            # 相对 input_dir 的路径
    size_bytes: int           # 文件字节大小
    kind: str                 # 内部类型: pdf/docx/txt/csv/xlsx/json/markdown/html/image/param/log/rlog/tlog/wps/code_*/unsupported
    file_id: str              # sha1(rel_path) 前 8 位
    mtime: _dt.datetime | None = None  # 修改时间（本地时区）
    ctime: _dt.datetime | None = None  # "创建时间"（见 ctime_source）
    ctime_source: str = "unavailable"  # birthtime / win_ctime / status_ctime / unavailable
    # 内容哈希（仅当 cfg.needs_content_hash=True 时由 hasher 填充）
    content_hash: str | None = None
    content_hash_algo: str | None = None
    content_hash_skipped_reason: str | None = None  # 例如 "size>FILE_HASH_MAX_BYTES" / "read_error: ..."

    # ---- 三层架构新增字段 ----
    # 「枚举层」：本结构所有实例都属于全量枚举。
    # 「进目录筛选层」：是否有资格进入 TOC 后续提取流水线。
    eligible: bool = True
    # 被拒原因列表（按 ext_filter / size_filter / mtime_filter / ctime_filter 分类）
    eligibility_reasons: list[str] = field(default_factory=list)

    @property
    def ext(self) -> str:
        return self.path.suffix.lower()

    @property
    def size_class(self) -> str:
        """对 LLM 友好的「数量级」标签，帮助快速判定文件是否大概率唯一。

        - <1KB / 1KB~1MB / 1MB~10MB / 10MB~100MB / >100MB
        - 大于 10MB 的文件，精确字节数几乎是唯一的；LLM 看到 size_class 就能立刻意识到。
        """
        b = self.size_bytes
        if b < 1024:
            return "<1KB"
        if b < 1024 * 1024:
            return "1KB-1MB"
        if b < 10 * 1024 * 1024:
            return "1MB-10MB"
        if b < 100 * 1024 * 1024:
            return "10MB-100MB"
        return ">100MB"


def _extract_creation_time(stat_result) -> tuple[_dt.datetime | None, str]:
    """获取文件"创建时间"（平台行为不一）。

    返回 (datetime, source)：
      - "birthtime"     : 文件系统真正提供的创建时间（macOS 总是；新内核+statx 的 Linux；BSD）
      - "win_ctime"     : Windows 上 st_ctime 即创建时间（语义上是创建时间）
      - "status_ctime"  : POSIX st_ctime（元数据变更时间，**不是真创建时间**），仅在没法拿
                           birthtime 时退化使用，并明确标注
      - "unavailable"   : 都拿不到
    """
    btime = getattr(stat_result, "st_birthtime", None)
    if btime:
        try:
            return _dt.datetime.fromtimestamp(btime), "birthtime"
        except (OverflowError, OSError, ValueError):
            pass
    if sys.platform.startswith("win"):
        # Windows: st_ctime 就是创建时间
        try:
            return _dt.datetime.fromtimestamp(stat_result.st_ctime), "win_ctime"
        except (OverflowError, OSError, ValueError, AttributeError):
            return None, "unavailable"
    # POSIX 退化：拿 st_ctime（status change time）作为参考，标注语义
    try:
        return _dt.datetime.fromtimestamp(stat_result.st_ctime), "status_ctime"
    except (OverflowError, OSError, ValueError, AttributeError):
        return None, "unavailable"


def _compute_file_id(rel_path: Path) -> str:
    """用相对路径的 sha1 前 8 位作为短 ID。"""
    h = hashlib.sha1(str(rel_path).encode("utf-8")).hexdigest()
    return h[:8]


def _passes_mtime_filter(
    mtime_dt: _dt.datetime,
    *,
    after: _dt.datetime | None,
    before: _dt.datetime | None,
) -> bool:
    """检查 mtime 是否在 [after, before] 闭区间内。两端任一为 None 表示不限。"""
    if after is not None and mtime_dt < after:
        return False
    if before is not None and mtime_dt > before:
        return False
    return True


def _passes_ctime_filter(
    ctime_dt: _dt.datetime | None,
    *,
    after: _dt.datetime | None,
    before: _dt.datetime | None,
) -> bool:
    """检查 ctime 是否在 [after, before] 闭区间内。两端任一为 None 表示不限。"""
    if ctime_dt is None:
        return True  # 拿不到 ctime → 默认放行
    if after is not None and ctime_dt < after:
        return False
    if before is not None and ctime_dt > before:
        return False
    return True


def scan(cfg: AppConfig) -> tuple[list[ScannedFile], list[ScannedFile]]:
    """三层架构的扫描：

    第 1 层（枚举）：遍历输入目录，所有文件全部收集（连 .env / __pycache__ 等都跳过隐藏前缀）；
                  这一层不做任何用户可配置的筛选，保证完整目录可用于 LLM 查重。
    第 2 层（进目录筛选）：对枚举结果应用所有 ext / size / mtime / ctime 筛选，
                       标记 `eligible` + 收集 `eligibility_reasons`。
                       被拒文件仍然在返回的 all_files 列表里，但 eligible=False。
    第 3 层（提取流水线）：调用方根据 `f.eligible` 决定哪些文件进 converter 流水线。

    返回 (all_files, eligible_files)：
      - all_files: 全量枚举结果（用于「完整文件目录」、查重等），按相对路径排序
      - eligible_files: all_files 中 eligible=True 的子集（其实就是 all_files 的视图,但提供出来便于调用）
    """
    if not cfg.input_dir.exists():
        raise SystemExit(f"[扫描错误] 输入目录不存在: {cfg.input_dir}")
    if not cfg.input_dir.is_dir():
        raise SystemExit(f"[扫描错误] 输入路径不是目录: {cfg.input_dir}")

    image_exts = cfg.image_exts_with_dot
    all_files: list[ScannedFile] = []

    size_catalog = getattr(cfg, "size_filter_catalog", None)
    size_filter_on = getattr(cfg, "size_filter_enabled", False)
    ext_filter = getattr(cfg, "eligibility_ext_filter", None)  # 可选的扩展名筛选
    ctime_filter_enabled = getattr(cfg, "ctime_filter_enabled", False)
    ctime_after = getattr(cfg, "ctime_after", None)
    ctime_before = getattr(cfg, "ctime_before", None)

    # 「枚举层物理跳过」清单：默认 + 用户在 .env 中追加
    purge_dirs = set(DEFAULT_PURGE_DIR_NAMES) | set(getattr(cfg, "purge_dir_names_extra", set()))
    purge_files = set(DEFAULT_PURGE_FILENAMES) | set(getattr(cfg, "purge_filenames_extra", set()))
    purge_suffixes = set(DEFAULT_PURGE_SUFFIXES) | set(getattr(cfg, "purge_suffixes_extra", set()))
    purge_count = 0

    # ---------- 第 1 层：纯枚举 ----------
    for path in cfg.input_dir.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(cfg.input_dir).parts

        # 跳过隐藏文件 / 任意父目录以 . 开头
        if any(part.startswith(".") for part in rel_parts):
            purge_count += 1
            continue

        # 跳过路径中含有「常见垃圾目录名」的文件
        if any(part in purge_dirs for part in rel_parts[:-1]):
            purge_count += 1
            continue

        # 跳过特定文件名（Thumbs.db / desktop.ini 等）
        if path.name in purge_files:
            purge_count += 1
            continue

        # 跳过特定后缀（.pyc / .swp / .lock 等）
        if path.suffix.lower() in purge_suffixes:
            purge_count += 1
            continue

        ext = path.suffix.lower()
        if ext in _EXT_TO_KIND:
            kind = _EXT_TO_KIND[ext]
        elif ext in image_exts:
            kind = "image"
        else:
            kind = "unsupported"

        try:
            stat = path.stat()
            size = stat.st_size
            mtime_dt = _dt.datetime.fromtimestamp(stat.st_mtime)
            ctime_dt, ctime_src = _extract_creation_time(stat)
        except OSError as e:
            logger.warning(f"无法读取文件元信息: {path}: {e}")
            size = 0
            mtime_dt = None
            ctime_dt = None
            ctime_src = "unavailable"

        rel = path.relative_to(cfg.input_dir)
        sf = ScannedFile(
            path=path,
            rel_path=rel,
            size_bytes=size,
            kind=kind,
            file_id=_compute_file_id(rel),
            mtime=mtime_dt,
            ctime=ctime_dt,
            ctime_source=ctime_src,
        )
        all_files.append(sf)

    # 排序：相对路径字典序，保证可复现
    all_files.sort(key=lambda f: str(f.rel_path).lower())

    # ---------- 第 2 层：进目录筛选（决定 eligible） ----------
    for sf in all_files:
        reasons: list[str] = []

        # 2.1 扩展名筛选（如果 cfg.eligibility_ext_filter 提供）
        if ext_filter is not None:
            ok, why = ext_filter.check(ext=sf.ext, kind=sf.kind)
            if not ok:
                reasons.append(f"ext_filter: {why}")

        # 2.2 大小筛选
        if size_filter_on and size_catalog is not None:
            ok, why = size_catalog.check(ext=sf.ext, kind=sf.kind, size_bytes=sf.size_bytes)
            if not ok:
                reasons.append(f"size_filter: {why}")

        # 2.3 mtime 筛选
        if cfg.mtime_filter_enabled and sf.mtime is not None:
            if not _passes_mtime_filter(
                sf.mtime, after=cfg.mtime_after, before=cfg.mtime_before
            ):
                reasons.append(
                    f"mtime_filter: {sf.mtime.isoformat()} 不在 "
                    f"[{cfg.mtime_after or '不限'}, {cfg.mtime_before or '不限'}]"
                )

        # 2.4 ctime 筛选（如启用）
        if ctime_filter_enabled and sf.ctime is not None:
            if not _passes_ctime_filter(
                sf.ctime, after=ctime_after, before=ctime_before
            ):
                src_note = f" ({sf.ctime_source})" if sf.ctime_source else ""
                reasons.append(
                    f"ctime_filter: {sf.ctime.isoformat()}{src_note} 不在 "
                    f"[{ctime_after or '不限'}, {ctime_before or '不限'}]"
                )

        if reasons:
            sf.eligible = False
            sf.eligibility_reasons = reasons

    eligible_files = [f for f in all_files if f.eligible]

    n_total = len(all_files)
    n_eligible = len(eligible_files)
    rejected_by: dict[str, int] = {}
    for f in all_files:
        if f.eligible:
            continue
        for r in f.eligibility_reasons:
            key = r.split(":", 1)[0]
            rejected_by[key] = rejected_by.get(key, 0) + 1

    if n_eligible < n_total:
        breakdown = ", ".join(f"{k}={v}" for k, v in sorted(rejected_by.items()))
        logger.info(
            f"扫描完成：物理跳过垃圾 {purge_count} 个 / 枚举 {n_total} 个文件，"
            f"{n_eligible} 个有资格进入提取（被拒 {n_total - n_eligible}：{breakdown}）"
        )
    else:
        logger.info(
            f"扫描完成：物理跳过垃圾 {purge_count} 个 / 枚举 {n_total} 个文件，"
            "全部有资格进入提取"
        )

    return all_files, eligible_files
