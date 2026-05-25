"""Windows PE 可执行文件 (.exe / .dll / .sys / .ocx) 头部解析转换器。

提取价值高的元信息：
- DOS 头识别（MZ 魔数）
- PE 头：架构（x86/x64/ARM/ARM64）、编译时间戳、Subsystem、特征位
- 节表（.text/.data/.rsrc/.reloc 等）—— 反映程序结构
- 末尾 1KB hex 预览（很多 PE 文件末尾是 PE-COFF Authenticode 签名）
- SHA-1 摘要（精确指纹）

不做完整反汇编（那是 LLM 的工作）；只做"机器可读元信息提取"。
"""
from __future__ import annotations

import hashlib
import logging
import struct
from pathlib import Path

from .base import BaseConverter, ConversionResult

logger = logging.getLogger(__name__)


# IMAGE_FILE_MACHINE 常量（节选）
_MACHINE: dict[int, str] = {
    0x0000: "UNKNOWN",
    0x014c: "x86 (i386)",
    0x0162: "MIPS R3000",
    0x0166: "MIPS R4000",
    0x0168: "MIPS R10000",
    0x0169: "MIPS WCE v2",
    0x0184: "Alpha",
    0x01a2: "SH3",
    0x01a3: "SH3DSP",
    0x01a6: "SH4",
    0x01a8: "SH5",
    0x01c0: "ARM",
    0x01c2: "ARM Thumb",
    0x01c4: "ARMv7 Thumb-2",
    0x01d3: "AM33",
    0x01f0: "PowerPC",
    0x01f1: "PowerPC FP",
    0x0200: "IA64 (Itanium)",
    0x0266: "MIPS16",
    0x0284: "Alpha 64",
    0x0366: "MIPS w/FPU",
    0x0466: "MIPS16 w/FPU",
    0x0ebc: "EFI Byte Code",
    0x8664: "x64 (AMD64)",
    0x9041: "M32R",
    0xaa64: "ARM64",
    0xc0ee: "CLR Pure MSIL",
}

# IMAGE_SUBSYSTEM
_SUBSYSTEM: dict[int, str] = {
    0: "UNKNOWN",
    1: "NATIVE (driver)",
    2: "WINDOWS_GUI",
    3: "WINDOWS_CUI (console)",
    5: "OS2_CUI",
    7: "POSIX_CUI",
    8: "NATIVE_WINDOWS",
    9: "WINDOWS_CE_GUI",
    10: "EFI_APPLICATION",
    11: "EFI_BOOT_SERVICE_DRIVER",
    12: "EFI_RUNTIME_DRIVER",
    13: "EFI_ROM",
    14: "XBOX",
    16: "WINDOWS_BOOT_APPLICATION",
}

# 主要的 Characteristics 位
_CHARACTERISTICS_BITS = [
    (0x0001, "RELOCS_STRIPPED"),
    (0x0002, "EXECUTABLE_IMAGE"),
    (0x0004, "LINE_NUMS_STRIPPED"),
    (0x0008, "LOCAL_SYMS_STRIPPED"),
    (0x0020, "LARGE_ADDRESS_AWARE"),
    (0x0100, "32BIT_MACHINE"),
    (0x0200, "DEBUG_STRIPPED"),
    (0x2000, "DLL"),
    (0x4000, "UP_SYSTEM_ONLY"),
]


def _hex_preview(data: bytes, *, bytes_per_line: int = 16) -> str:
    if not data:
        return "(空)\n"
    lines = []
    for offset in range(0, len(data), bytes_per_line):
        chunk = data[offset : offset + bytes_per_line]
        hex_part = " ".join(f"{b:02x}" for b in chunk)
        ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{offset:08x}  {hex_part:<{bytes_per_line * 3 - 1}}  |{ascii_part}|")
    return "\n".join(lines) + "\n"


def _decode_characteristics(value: int) -> str:
    flags = [name for bit, name in _CHARACTERISTICS_BITS if value & bit]
    return ", ".join(flags) if flags else "(无)"


def _parse_pe(raw: bytes) -> tuple[dict, list[str]]:
    """解析 PE 文件头。返回 (info_dict, warnings)。

    解析失败时 info_dict 至少有 'magic' 字段说明问题。
    """
    info: dict = {}
    warnings: list[str] = []

    if len(raw) < 64:
        warnings.append("文件长度不足，无法包含 DOS 头")
        return info, warnings

    # DOS 头
    if raw[:2] != b"MZ":
        info["magic"] = f"非 MZ 魔数（实际 {raw[:2].hex()}），不是 PE 文件"
        return info, warnings
    info["magic"] = "MZ"

    e_lfanew = struct.unpack_from("<I", raw, 0x3C)[0]
    info["e_lfanew"] = e_lfanew
    if e_lfanew + 24 > len(raw):
        warnings.append(f"e_lfanew=0x{e_lfanew:x} 指向越界位置，无法读取 PE 头")
        return info, warnings

    pe_sig = raw[e_lfanew : e_lfanew + 4]
    info["pe_signature"] = pe_sig.hex()
    if pe_sig != b"PE\x00\x00":
        warnings.append(f"PE 签名不正确（实际 {pe_sig.hex()}）")
        return info, warnings

    # COFF 文件头（紧跟 PE 签名）：Machine(2) NumOfSections(2) TimeDateStamp(4) ... Characteristics(2)
    coff_off = e_lfanew + 4
    if coff_off + 20 > len(raw):
        warnings.append("COFF 头被截断")
        return info, warnings
    (
        machine, n_sections, time_stamp,
        ptr_to_sym, n_syms, size_of_opt_header, characteristics,
    ) = struct.unpack_from("<HHIIIHH", raw, coff_off)
    info["machine_raw"] = machine
    info["machine"] = _MACHINE.get(machine, f"0x{machine:04x}")
    info["num_sections"] = n_sections
    info["timestamp_unix"] = time_stamp
    info["characteristics_raw"] = characteristics
    info["characteristics"] = _decode_characteristics(characteristics)
    info["size_of_optional_header"] = size_of_opt_header

    # Optional Header（PE32 / PE32+）
    opt_off = coff_off + 20
    if size_of_opt_header > 0 and opt_off + 2 <= len(raw):
        magic = struct.unpack_from("<H", raw, opt_off)[0]
        if magic == 0x10b:
            info["pe_format"] = "PE32"
            opt_layout = "<HBBIIIIIIIIIHHHHHHIIIIHHIIIIII"
        elif magic == 0x20b:
            info["pe_format"] = "PE32+ (64-bit)"
            opt_layout = "<HBBIIIIIQQIHHHHHHIIIIHHQQQQII"
        else:
            info["pe_format"] = f"未知 (0x{magic:x})"
            opt_layout = None
        if opt_layout and opt_off + struct.calcsize(opt_layout) <= len(raw):
            try:
                vals = struct.unpack_from(opt_layout, raw, opt_off)
                # PE32 与 PE32+ 共享前 20 个字段，Subsystem 索引位置略不同
                # 这里只摘几个最有价值的：MajorLinkerVer、SizeOfImage、Subsystem、ImageBase
                info["linker_version"] = f"{vals[1]}.{vals[2]}"
                info["size_of_code"] = vals[3]
                info["entry_point_rva"] = vals[6]
                # Subsystem 位置：PE32 在第 22 项（vals[22]），PE32+ 也是第 22 项
                # 由于布局不同字段含义，但 Subsystem 字段都在 SizeOfHeaders 之后，
                # 我们用通用偏移：opt_off + 68 (PE32) 或 opt_off + 68 (PE32+) 都是 Subsystem
                subsys_off = opt_off + 68
                if subsys_off + 2 <= len(raw):
                    sub = struct.unpack_from("<H", raw, subsys_off)[0]
                    info["subsystem"] = _SUBSYSTEM.get(sub, f"0x{sub:04x}")
            except Exception as e:
                warnings.append(f"Optional Header 解析部分失败：{e}")

    # 节表
    section_off = opt_off + size_of_opt_header
    sections: list[dict] = []
    for i in range(min(n_sections, 32)):  # 安全上限 32
        off = section_off + i * 40
        if off + 40 > len(raw):
            warnings.append(f"节表第 {i} 项被截断")
            break
        name = raw[off : off + 8].rstrip(b"\x00")
        try:
            name_str = name.decode("ascii", errors="replace")
        except Exception:
            name_str = name.hex()
        vsize, vaddr, rsize, raddr = struct.unpack_from("<IIII", raw, off + 8)
        sections.append({
            "name": name_str, "vsize": vsize, "vaddr": vaddr,
            "rsize": rsize, "raddr": raddr,
        })
    info["sections"] = sections

    return info, warnings


class ExeConverter(BaseConverter):
    """PE 可执行文件 (.exe / .dll / .sys / .ocx) 转换器。"""

    kind = "exe"

    def convert(self, path: Path) -> ConversionResult:
        try:
            size = path.stat().st_size
        except OSError as e:
            return ConversionResult(
                status="failed", markdown="",
                error=f"读取失败: {e.__class__.__name__}: {e}",
            )

        # 只读头部（足够 PE 结构）+ 尾部 hex 预览
        HEAD_BYTES = 8192   # 8KB 足够覆盖 DOS+PE+OptionalHeader+若干节表
        TAIL_BYTES = 512
        try:
            with path.open("rb") as f:
                head = f.read(HEAD_BYTES)
                if size > HEAD_BYTES + TAIL_BYTES:
                    f.seek(max(0, size - TAIL_BYTES))
                    tail = f.read(TAIL_BYTES)
                else:
                    tail = b""
        except OSError as e:
            return ConversionResult(
                status="failed", markdown="",
                error=f"读取失败: {e.__class__.__name__}: {e}",
            )

        # SHA-1 全文件摘要
        try:
            with path.open("rb") as f:
                sha1 = hashlib.sha1()
                while True:
                    chunk = f.read(1024 * 1024)
                    if not chunk:
                        break
                    sha1.update(chunk)
            sha1_hex = sha1.hexdigest()
        except Exception:
            sha1_hex = "(计算失败)"

        info, warnings = _parse_pe(head)

        # 拼装 markdown
        parts: list[str] = []
        parts.append("**Windows PE 可执行文件（仅头部元信息）**\n\n")
        parts.append(f"- 文件大小: {size:,} bytes\n")
        parts.append(f"- SHA-1: `{sha1_hex}`\n")
        parts.append(f"- DOS magic: `{info.get('magic', '?')}`\n")

        if info.get("magic") == "MZ" and "machine" in info:
            import datetime as _dt
            try:
                ts_iso = _dt.datetime.utcfromtimestamp(info["timestamp_unix"]).isoformat() + "Z"
            except Exception:
                ts_iso = "(无效时间戳)"
            parts.append(f"- 架构: **{info['machine']}**（raw=0x{info['machine_raw']:04x}）\n")
            parts.append(f"- 编译时间戳: {info['timestamp_unix']} → {ts_iso}\n")
            parts.append(f"- PE 格式: {info.get('pe_format', '?')}\n")
            if "linker_version" in info:
                parts.append(f"- 链接器版本: {info['linker_version']}\n")
            if "subsystem" in info:
                parts.append(f"- Subsystem: {info['subsystem']}\n")
            if "entry_point_rva" in info:
                parts.append(f"- 入口点 RVA: 0x{info['entry_point_rva']:x}\n")
            parts.append(f"- 特征位 (0x{info['characteristics_raw']:04x}): {info['characteristics']}\n")
            parts.append(f"- 节数量: {info['num_sections']}\n\n")

            if info.get("sections"):
                parts.append("### 节表\n\n")
                parts.append("| # | 名称 | VirtualSize | VirtualAddr | RawSize | RawAddr |\n")
                parts.append("|---|------|-------------|-------------|---------|--------|\n")
                for i, sec in enumerate(info["sections"], start=1):
                    parts.append(
                        f"| {i} | `{sec['name']}` | {sec['vsize']:,} | 0x{sec['vaddr']:x} | "
                        f"{sec['rsize']:,} | 0x{sec['raddr']:x} |\n"
                    )
                parts.append("\n")
        else:
            parts.append("\n> ⚠️ 不是有效的 PE 文件（或文件过小），仅给出 hex 预览。\n\n")

        parts.append(f"### 头部 hex（前 {min(256, len(head))} bytes）\n\n```\n")
        parts.append(_hex_preview(head[:256]))
        parts.append("```\n\n")

        if tail:
            parts.append(f"### 尾部 hex（最后 {len(tail)} bytes，常含 Authenticode 签名）\n\n```\n")
            parts.append(_hex_preview(tail))
            parts.append("```\n")

        return ConversionResult(
            status="success",
            markdown="".join(parts),
            warnings=warnings,
            extra={
                "original_bytes": size,
                "sha1": sha1_hex,
                "machine": info.get("machine", ""),
                "pe_format": info.get("pe_format", ""),
                "subsystem": info.get("subsystem", ""),
                "num_sections": info.get("num_sections", 0),
                "timestamp_unix": info.get("timestamp_unix", 0),
            },
        )
