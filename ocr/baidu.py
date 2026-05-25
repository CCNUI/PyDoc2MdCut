"""百度文字识别 OCR 客户端。

封装 access_token 获取/缓存、单图调用、限流、重试逻辑。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import requests

logger = logging.getLogger(__name__)


# 限流/可恢复错误码（百度官方文档）
# 17  Open api daily request limit reached
# 18  Open api qps request limit reached
# 19  Open api total request limit reached
# 4   Open api request limit reached（部分接口）
_RETRYABLE_ERROR_CODES = {4, 17, 18, 19}

# 需要刷新 token 的错误码
# 110 Access token invalid or no longer valid
# 111 Access token expired
_TOKEN_INVALID_ERROR_CODES = {110, 111}


_TOKEN_URL = "https://aip.baidubce.com/oauth/2.0/token"
_OCR_URL_ACCURATE = "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic"
_OCR_URL_GENERAL = "https://aip.baidubce.com/rest/2.0/ocr/v1/general_basic"


class OCRError(Exception):
    """OCR 调用异常。包含百度错误码（如有）以便上游记录。"""

    def __init__(self, message: str, error_code: int | None = None) -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass
class OCRResult:
    """OCR 单图识别结果。"""
    text: str                    # 拼接后的文本
    char_count: int              # 字符数
    elapsed_seconds: float       # 调用耗时
    engine: str                  # 使用的引擎
    raw_lines: int               # 识别到的行数


def _hash_api_key(api_key: str) -> str:
    """API Key 的 sha1 前 8 位，用于缓存校验。"""
    return hashlib.sha1(api_key.encode("utf-8")).hexdigest()[:8]


class BaiduOCRClient:
    """百度 OCR 客户端。

    使用方式::

        client = BaiduOCRClient(api_key=..., secret_key=..., engine="accurate_basic", ...)
        result = client.recognize_image(Path("photo.jpg"))
    """

    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        engine: str = "accurate_basic",
        max_retries: int = 3,
        min_interval: float = 0.5,
        token_cache_path: Path,
        max_image_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if not api_key or not secret_key:
            raise OCRError("百度 OCR API Key 或 Secret Key 为空")
        self.api_key = api_key
        self.secret_key = secret_key
        self.engine = engine
        self.max_retries = max_retries
        self.min_interval = min_interval
        self.token_cache_path = token_cache_path
        self.max_image_bytes = max_image_bytes

        self._access_token: str | None = None
        self._last_call_ts: float = 0.0
        self._session = requests.Session()

    # ---------------- token 管理 ----------------
    def _load_cached_token(self) -> str | None:
        """从缓存文件读 token；不可用返回 None。"""
        try:
            if not self.token_cache_path.exists():
                return None
            data = json.loads(self.token_cache_path.read_text(encoding="utf-8"))
            if data.get("api_key_hash") != _hash_api_key(self.api_key):
                logger.debug("OCR token 缓存：API key 已更换，弃用旧缓存")
                return None
            # 留 1 天余量
            if data.get("expires_at", 0) < time.time() + 86400:
                logger.debug("OCR token 缓存：即将过期，弃用旧缓存")
                return None
            token = data.get("access_token")
            if isinstance(token, str) and token:
                logger.debug("OCR token 缓存命中")
                return token
            return None
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"OCR token 缓存读取失败: {e}")
            return None

    def _save_cached_token(self, token: str, expires_in: int) -> None:
        """expires_in 来自百度返回（单位秒，约 30 天）。"""
        data = {
            "access_token": token,
            "expires_at": int(time.time()) + int(expires_in),
            "api_key_hash": _hash_api_key(self.api_key),
        }
        try:
            self.token_cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.token_cache_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            logger.debug(f"OCR token 已缓存到 {self.token_cache_path}")
        except OSError as e:
            logger.warning(f"OCR token 缓存写入失败: {e}")

    def _fetch_new_token(self) -> str:
        """调用 OAuth 接口获取新 token。"""
        params = {
            "grant_type": "client_credentials",
            "client_id": self.api_key,
            "client_secret": self.secret_key,
        }
        logger.debug("请求百度 OAuth token ...")
        try:
            resp = self._session.post(_TOKEN_URL, params=params, timeout=20)
        except requests.RequestException as e:
            raise OCRError(f"获取 access_token 失败（网络错误）: {e}") from e

        if resp.status_code != 200:
            raise OCRError(
                f"获取 access_token 失败 HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise OCRError(f"获取 access_token 失败（响应非 JSON）: {e}") from e

        if "error" in data:
            raise OCRError(
                f"获取 access_token 失败: {data.get('error')} - "
                f"{data.get('error_description', '')}"
            )

        token = data.get("access_token")
        expires_in = data.get("expires_in", 30 * 24 * 3600)
        if not token:
            raise OCRError("获取 access_token 失败：响应中缺少 access_token 字段")

        self._save_cached_token(token, expires_in)
        return token

    def _ensure_token(self, *, force_refresh: bool = False) -> str:
        """确保有可用 token，优先用缓存。"""
        if self._access_token and not force_refresh:
            return self._access_token
        if not force_refresh:
            cached = self._load_cached_token()
            if cached:
                self._access_token = cached
                return cached
        token = self._fetch_new_token()
        self._access_token = token
        return token

    # ---------------- 限流 ----------------
    def _throttle(self) -> None:
        """两次调用间至少间隔 min_interval 秒。"""
        if self.min_interval <= 0:
            return
        elapsed = time.time() - self._last_call_ts
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    # ---------------- 主接口 ----------------
    def recognize_image(self, image_path: Path) -> OCRResult:
        """识别单张图片，返回 OCRResult。

        Raises:
            OCRError: 重试用尽仍失败、文件超过 4MB、参数错误等。
        """
        if not image_path.exists():
            raise OCRError(f"图片不存在: {image_path}")

        size = image_path.stat().st_size
        if size > self.max_image_bytes:
            raise OCRError(
                f"图片超过 {self.max_image_bytes} bytes ({size} bytes)，"
                "百度 OCR 限制 4MB，已跳过"
            )

        try:
            image_bytes = image_path.read_bytes()
        except OSError as e:
            raise OCRError(f"读取图片失败: {e}") from e

        b64_image = base64.b64encode(image_bytes).decode("ascii")
        url = _OCR_URL_ACCURATE if self.engine == "accurate_basic" else _OCR_URL_GENERAL

        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            token = self._ensure_token()
            full_url = f"{url}?access_token={token}"

            t0 = time.time()
            try:
                resp = self._session.post(
                    full_url,
                    data={"image": b64_image},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=30,
                )
                self._last_call_ts = time.time()
            except requests.RequestException as e:
                last_exc = e
                logger.debug(
                    f"OCR 网络异常 attempt={attempt + 1}/{self.max_retries + 1}: {e}"
                )
                self._backoff(attempt)
                continue

            # HTTP 5xx → 重试
            if resp.status_code >= 500:
                last_exc = OCRError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                logger.debug(f"OCR 服务端错误 HTTP {resp.status_code}, 重试中")
                self._backoff(attempt)
                continue
            if resp.status_code != 200:
                # 其他 HTTP 错误：不重试
                raise OCRError(
                    f"OCR HTTP 错误 {resp.status_code}: {resp.text[:200]}"
                )

            try:
                data = resp.json()
            except ValueError as e:
                raise OCRError(f"OCR 响应非 JSON: {e}") from e

            # 业务错误码处理
            if "error_code" in data:
                code = data.get("error_code")
                msg = data.get("error_msg", "")
                if code in _TOKEN_INVALID_ERROR_CODES:
                    logger.info(f"OCR token 失效 (code={code})，刷新后重试")
                    self._ensure_token(force_refresh=True)
                    continue
                if code in _RETRYABLE_ERROR_CODES:
                    last_exc = OCRError(f"百度限流/配额错误 code={code}: {msg}", error_code=code)
                    logger.debug(f"OCR 可重试错误 code={code} msg={msg}")
                    self._backoff(attempt)
                    continue
                # 不可重试：直接抛
                raise OCRError(f"百度 OCR 错误 code={code}: {msg}", error_code=code)

            # 成功：解析 words_result
            elapsed = time.time() - t0
            words = data.get("words_result", []) or []
            lines = [w.get("words", "") for w in words]
            text = "\n".join(lines)
            logger.debug(
                f"OCR 成功: {image_path.name} 行数={len(lines)} 字符={len(text)} "
                f"耗时={elapsed:.2f}s engine={self.engine}"
            )
            return OCRResult(
                text=text,
                char_count=len(text),
                elapsed_seconds=elapsed,
                engine=self.engine,
                raw_lines=len(lines),
            )

        # 重试用尽
        raise OCRError(f"OCR 重试 {self.max_retries} 次仍失败: {last_exc}")

    @staticmethod
    def _backoff(attempt: int) -> None:
        """指数退避：1s, 2s, 4s, 8s ..."""
        time.sleep(min(2 ** attempt, 30))
