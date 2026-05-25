"""文件内容哈希计算模块。

仅当 cfg.needs_content_hash 为真时才会被调用。会就地修改 ScannedFile：
  - content_hash:        完整哈希（hex 字符串）
  - content_hash_algo:   实际使用的算法
  - content_hash_skipped_reason: 跳过原因（若被跳过则填，hash 字段保留 None）

为节省内存，分块读取（默认 1 MiB 块）。哈希出错只对单文件标记 skip，不中断流水线。
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from tqdm import tqdm

from config import AppConfig
from scanner import ScannedFile

logger = logging.getLogger(__name__)

_CHUNK = 1024 * 1024  # 1 MiB


def _new_hasher(algo: str):
    if algo == "sha256":
        return hashlib.sha256()
    if algo == "sha1":
        return hashlib.sha1()
    if algo == "md5":
        return hashlib.md5()
    raise ValueError(f"未知哈希算法: {algo}")


def _hash_file(path: Path, algo: str) -> str:
    h = _new_hasher(algo)
    with path.open("rb") as f:
        while True:
            buf = f.read(_CHUNK)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def compute_hashes(cfg: AppConfig, files: list[ScannedFile]) -> None:
    """就地填充 files 列表中每个 ScannedFile 的 content_hash。"""
    if not cfg.needs_content_hash:
        return
    if not files:
        return

    algo = cfg.file_hash_algo
    max_bytes = cfg.file_hash_max_bytes  # 0 表示不限

    skipped_oversize = 0
    failed = 0
    ok = 0

    iterator = tqdm(files, desc=f"计算哈希({algo})", unit="file")
    for sf in iterator:
        # 文件不存在 / 0 字节 / 元信息读取失败的兜底
        if sf.size_bytes == 0:
            try:
                # 0 字节文件也能算，结果是空哈希常量；保留确切值便于查重
                sf.content_hash = _hash_file(sf.path, algo)
                sf.content_hash_algo = algo
                ok += 1
            except OSError as e:
                sf.content_hash_skipped_reason = f"read_error: {e}"
                failed += 1
            continue

        if max_bytes > 0 and sf.size_bytes > max_bytes:
            sf.content_hash_skipped_reason = (
                f"size>{max_bytes}（FILE_HASH_MAX_BYTES）"
            )
            skipped_oversize += 1
            continue

        try:
            sf.content_hash = _hash_file(sf.path, algo)
            sf.content_hash_algo = algo
            ok += 1
        except (OSError, PermissionError) as e:
            sf.content_hash_skipped_reason = f"read_error: {e}"
            failed += 1
            logger.warning(f"哈希失败 {sf.rel_path}: {e}")

    logger.info(
        f"哈希计算完成：成功 {ok}，按大小跳过 {skipped_oversize}，失败 {failed}（算法 {algo}）"
    )
