"""按扩展名 / kind 的资格筛选。

这一层的目的：从「全量枚举」中挑选出有资格进入提取流水线的文件。
通常的用法：
- 默认：放行所有 ext / kind
- whitelist：只放行 include 中列出的 ext / kind，其余拒绝
- blacklist：拒绝 exclude 中列出的 ext / kind，其余放行
- 二者同时配置时，blacklist 优先（已经在白名单中也会被 blacklist 拒绝）
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ExtensionFilter:
    """扩展名 / kind 的进目录筛选规则。

    Attributes:
        include_exts: 仅放行的扩展名集合（如 {'.log','.txt'}）；为空集合表示「放行所有」
        include_kinds: 仅放行的 kind 集合；为空集合表示「放行所有」
        exclude_exts: 拒绝的扩展名集合
        exclude_kinds: 拒绝的 kind 集合
        unsupported_policy: 对 kind=='unsupported' 文件的策略
            - "allow"  : 允许进入（保持原有行为：占位 + 在报告中标"跳过"）
            - "reject" : 直接在第 2 层拒绝（更干净，但失去"跳过列表"信息）
    """
    include_exts: set[str] = field(default_factory=set)
    include_kinds: set[str] = field(default_factory=set)
    exclude_exts: set[str] = field(default_factory=set)
    exclude_kinds: set[str] = field(default_factory=set)
    unsupported_policy: str = "allow"

    def check(self, *, ext: str, kind: str) -> tuple[bool, str]:
        ext_l = (ext or "").lower()
        # blacklist 优先
        if ext_l in self.exclude_exts:
            return False, f"扩展名 {ext_l} 在 exclude 列表中"
        if kind in self.exclude_kinds:
            return False, f"kind {kind} 在 exclude 列表中"
        # whitelist：非空时仅放行列表内的
        if self.include_exts and ext_l not in self.include_exts:
            return False, f"扩展名 {ext_l} 不在 include 列表中"
        if self.include_kinds and kind not in self.include_kinds:
            return False, f"kind {kind} 不在 include 列表中"
        # unsupported 单独策略
        if kind == "unsupported" and self.unsupported_policy == "reject":
            return False, "unsupported_policy=reject"
        return True, ""

    def describe(self) -> str:
        parts = []
        if self.include_exts:
            parts.append(f"include_exts={sorted(self.include_exts)}")
        if self.include_kinds:
            parts.append(f"include_kinds={sorted(self.include_kinds)}")
        if self.exclude_exts:
            parts.append(f"exclude_exts={sorted(self.exclude_exts)}")
        if self.exclude_kinds:
            parts.append(f"exclude_kinds={sorted(self.exclude_kinds)}")
        if self.unsupported_policy != "allow":
            parts.append(f"unsupported_policy={self.unsupported_policy}")
        return "；".join(parts) if parts else "全部放行"
