"""按文件类型的大小筛选。

作用于 scanner 阶段：根据「文件类型 + 字节大小」决定是否纳入处理。

配置语义：
- min_mb / max_mb 单位是 MB（支持 0 / 小数 / 整数）
- 任一字段为 -1（或 .env 中写 false/off/no/none/null）→ 该项筛选关闭
- min_mb=0  表示「不限下限」（语义化更直观，0 不会过滤任何文件）
- max_mb=0  字面意义为「大于 0 字节即拒绝」，实际不常用；这里同样按字面执行
- 区间是闭区间：min <= size <= max
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SizeFilterSpec:
    """单类型的大小筛选规格。"""
    min_mb: float = -1.0  # -1 = 不限
    max_mb: float = -1.0  # -1 = 不限

    @property
    def min_bytes(self) -> int | None:
        """None 表示不限；其他返回字节数。0 也表示不限（人类直觉）。"""
        if self.min_mb < 0:
            return None
        if self.min_mb == 0:
            return None
        return int(self.min_mb * 1024 * 1024)

    @property
    def max_bytes(self) -> int | None:
        """None 表示不限；其他返回字节数。"""
        if self.max_mb < 0:
            return None
        return int(self.max_mb * 1024 * 1024)

    def is_meaningful(self) -> bool:
        return self.min_bytes is not None or self.max_bytes is not None

    def check(self, size_bytes: int) -> tuple[bool, str]:
        """返回 (passes, reason_if_rejected)。"""
        mn = self.min_bytes
        mx = self.max_bytes
        if mn is not None and size_bytes < mn:
            return False, f"小于下限 {self.min_mb} MB（{size_bytes:,} 字节）"
        if mx is not None and size_bytes > mx:
            return False, f"超过上限 {self.max_mb} MB（{size_bytes:,} 字节）"
        return True, ""

    def describe(self) -> str:
        if not self.is_meaningful():
            return "不限"
        parts = []
        if self.min_bytes is not None:
            parts.append(f"≥ {self.min_mb} MB")
        if self.max_bytes is not None:
            parts.append(f"≤ {self.max_mb} MB")
        return " 且 ".join(parts)


@dataclass
class SizeFilterCatalog:
    """统一管理所有类型的 SizeFilterSpec。

    使用方式：
        ok, reason = catalog.check(ext='.log', kind='log', size=12345)
    """
    by_ext: dict[str, SizeFilterSpec] = field(default_factory=dict)
    by_kind: dict[str, SizeFilterSpec] = field(default_factory=dict)
    default_spec: SizeFilterSpec = field(default_factory=SizeFilterSpec)
    enabled: bool = True

    def spec_for(self, *, ext: str, kind: str) -> SizeFilterSpec:
        ext_key = (ext or "").lower()
        if ext_key in self.by_ext:
            return self.by_ext[ext_key]
        if kind in self.by_kind:
            return self.by_kind[kind]
        return self.default_spec

    def check(self, *, ext: str, kind: str, size_bytes: int) -> tuple[bool, str]:
        if not self.enabled:
            return True, ""
        spec = self.spec_for(ext=ext, kind=kind)
        if not spec.is_meaningful():
            return True, ""
        return spec.check(size_bytes)

    def known_keys_describe(self) -> list[str]:
        out: list[str] = []
        for ext, sp in sorted(self.by_ext.items()):
            out.append(f"  ext  {ext:<10} → {sp.describe()}")
        # 只显示「真正起作用」的 kind 配置以减少日志噪声
        meaningful_kinds = [
            (k, sp) for k, sp in self.by_kind.items() if sp.is_meaningful()
        ]
        for kind, sp in sorted(meaningful_kinds):
            out.append(f"  kind {kind:<10} → {sp.describe()}")
        out.append(f"  default            → {self.default_spec.describe()}")
        return out
