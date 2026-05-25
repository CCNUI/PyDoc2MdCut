"""代码 / 配置类文件转换器（.py / .sh / .js / .yaml 等）。

行为与 TxtConverter 类似，但根据 kind 选择不同的 fenced 代码块语言标签，
让 LLM 能正确识别语法。所有 code_* kind 共用本 converter。
"""
from __future__ import annotations

import logging
from pathlib import Path

from charset_normalizer import from_bytes

from .base import BaseConverter, ConversionResult

logger = logging.getLogger(__name__)


# kind → fenced 代码块语言标签
_KIND_LANG: dict[str, str] = {
    "code_py": "python",
    "code_sh": "bash",
    "code_js": "javascript",
    "code_ts": "typescript",
    "code_java": "java",
    "code_c": "c",
    "code_cpp": "cpp",
    "code_cs": "csharp",
    "code_go": "go",
    "code_rs": "rust",
    "code_rb": "ruby",
    "code_php": "php",
    "code_swift": "swift",
    "code_kt": "kotlin",
    "code_scala": "scala",
    "code_m": "objectivec",
    "code_mm": "objectivec",
    "code_r": "r",
    "code_sql": "sql",
    "code_yaml": "yaml",
    "code_toml": "toml",
    "code_ini": "ini",
    "code_xml": "xml",
    "code_css": "css",
    "code_dockerfile": "dockerfile",
}


def make_code_converter(kind: str):
    """根据 kind 动态生成对应的 CodeConverter 子类。"""
    lang = _KIND_LANG.get(kind, "text")

    class _CodeConverter(BaseConverter):
        # 类属性 kind 由下面赋值
        def convert(self, path: Path) -> ConversionResult:
            try:
                raw = path.read_bytes()
            except OSError as e:
                return ConversionResult(
                    status="failed",
                    markdown="",
                    error=f"读取失败: {e.__class__.__name__}: {e}",
                )

            # pre-read 截取（对大代码文件有意义）
            raw, _info, pre_warnings = self._pre_read_truncate_bytes(raw, path=path)

            text: str | None = None
            encoding = "utf-8"
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                best = from_bytes(raw).best()
                if best is not None:
                    text = str(best)
                    encoding = best.encoding or "unknown"
                else:
                    text = raw.decode("utf-8", errors="replace")
                    encoding = "utf-8(replace)"

            warnings: list[str] = list(pre_warnings)
            if encoding not in ("utf-8", "utf_8", "ascii"):
                warnings.append(f"原文件编码非 UTF-8（探测为 {encoding}），已转码")

            md = f"```{lang}\n{text}\n```\n"
            return ConversionResult(
                status="success",
                markdown=md,
                warnings=warnings,
                extra={"chars": len(text), "encoding": encoding, "lang": lang},
            )

    _CodeConverter.kind = kind
    _CodeConverter.__name__ = f"CodeConverter_{kind}"
    return _CodeConverter


# 预先实例化所有 code_* 类型，方便 convert.py 引用
ALL_CODE_KINDS = tuple(_KIND_LANG.keys())
