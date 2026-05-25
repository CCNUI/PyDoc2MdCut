# 文档批量转换 + 合并 + 分卷工具

把一个项目/资料夹的全部内容转换成 Markdown，按 **19 MB / 卷**（可配置）分卷输出，
专为「分批喂给大语言模型作为 Prompt 上下文」设计。

- 图片走 **百度 OCR API**（高精度 / 标准版可选）
- 跨平台：**Windows 11 (x86_64)** 与 **统信 UOS（ARM64 / 海思麒麟9000）** 均可运行
- 单文件失败不会中断流水线；最终生成 `conversion_report.md` 一份完整报告

## 三层架构（核心设计）

为了处理「上万个大文件」的批量上传 LLM 场景，工具采用三层处理架构：

```
┌──────────────────────────────────────────────────────────────────┐
│ 第 1 层：枚举（不受任何用户配置的筛选影响）                       │
│   - 递归扫描输入目录的全部文件                                    │
│   - 对每个文件记录：path / size_bytes / size_class / mtime / ctime │
│   - 输出「完整文件目录」到合并 MD 顶部 + conversion_report.md    │
│   - 大小用 **byte 精度** + 数量级标签（<1KB / 1MB-10MB / >100MB）  │
│   - 目的：给 LLM 一份不可变的全量视图，便于按 (路径,字节数) 查重    │
└──────────────────────────────────────────────────────────────────┘
                                ↓
┌──────────────────────────────────────────────────────────────────┐
│ 第 2 层：进目录筛选（决定哪些文件有资格进入提取流水线）            │
│   - 扩展名 / kind 白名单 / 黑名单（ELIGIBILITY_*）                 │
│   - 大小范围筛选（SIZE_FILTER_*，按扩展名/kind 独立配置）         │
│   - 修改日期筛选（MTIME_*）                                       │
│   - 创建日期筛选（CTIME_*）                                       │
│   - 每个被拒文件仍出现在第 1 层目录里，标 ❌ 并附拒因              │
└──────────────────────────────────────────────────────────────────┘
                                ↓
┌──────────────────────────────────────────────────────────────────┐
│ 第 3 层：提取流水线                                               │
│   - 各类型 converter 转 markdown                                  │
│   - 通用截取（TRUNC_*）：头 + 尾 + 中间随机抽样                    │
│   - 合并 + 分卷                                                  │
└──────────────────────────────────────────────────────────────────┘
```

---

## 支持的文件类型

| 扩展名 | 处理方式 |
| --- | --- |
| `.pdf` | `pdfplumber` 提取文本；自动检测图片型 PDF 并发出警告 |
| `.docx` | `mammoth` 首选 → 失败时 `python-docx` 兜底 |
| `.txt` | UTF-8 优先，其他编码用 `charset-normalizer` 自动探测 |
| `.csv` | `pandas` → Markdown 表格（超过 `CSV_MAX_ROWS` 截断） |
| `.xlsx` | 每个 sheet 一个二级标题 + 表格 |
| `.json` | 美化后包进 ` ```json ` 代码块 |
| `.md` `.markdown` | 原样保留 |
| `.html` `.htm` | `markdownify` → Markdown |
| `.png .jpg .jpeg .gif .webp .bmp .tiff` | **百度 OCR**（需要 API key） |
| **`.param`** *(新)* | MissionPlanner / ArduPilot 参数备份 → 解析为表格 + 原文 |
| **`.log`** *(新)* | 通用文本日志，受 `LOG_FILE_MAX_BYTES` 头+尾截取 |
| **`.rlog`** *(新)* | 自动识别文本/二进制；二进制走 hex 预览 + SHA-1 |
| **`.tlog`** *(新)* | MAVLink 遥测：装了 `pymavlink` 解码每条消息；否则元信息+hex 兜底 |
| **`.wps`** *(新)* | 金山 WPS 文档：OOXML→mammoth ／ OLE→LibreOffice→mammoth ／ olefile 兜底 |
| **代码 / 配置类** *(新)* | `.py .sh .bash .js .ts .jsx .tsx .java .c .h .cpp .hpp .cc .cs .go .rs .rb .php .swift .kt .scala .m .mm .r .sql .yaml .yml .toml .ini .cfg .conf .xml .css .scss .less .dockerfile` —— 自动按语言加 fenced 代码块标签 |

不支持的扩展名：跳过 + 记入报告，不报错。

---

## 安装

```bash
# 1. 创建虚拟环境（可选但强烈推荐）
python3.12 -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate

# 2. 安装核心依赖
pip install -r requirements.txt

# 3. （可选）装可选依赖：解码 .tlog、读取图片尺寸
pip install -r requirements-optional.txt
```

### 平台说明

- **Windows 11 (x86_64)**：所有依赖均有官方 wheel，开箱即用。
- **统信 UOS (ARM64 / 海思麒麟9000)**：所有核心依赖在 ARM64 Linux 上均有 wheel 或纯 Python。
  万一某包要编译，先 `sudo apt install build-essential python3-dev libffi-dev` 再重试。
- **`.wps` OLE 路径** 需要本地 LibreOffice。Windows 版安装包：
  https://www.libreoffice.org/download/ ；UOS / Debian: `sudo apt install libreoffice`。
  没安装也不会报错，会回落到 olefile 粗略文本抽取或失败占位。

---

## 配置

复制 `.env.example` 为 `.env` 后按需填入：

```bash
cp .env.example .env
# Windows: copy .env.example .env
```

> ⚠️ `.env` 已写入 `.gitignore`，请勿把真实凭据提交到仓库。

### 申请百度 OCR API key（启用图片识别才需要）

1. 注册并登录 **百度智能云**：https://cloud.baidu.com/
2. 控制台 → 文字识别 → 应用列表：
   https://console.bce.baidu.com/ai/#/ai/ocr/app/list
3. 「创建应用」→ 应用类型选「文字识别」→ 至少勾选「通用文字识别（高精度版）」与
   「通用文字识别（标准版）」中的一项 → 提交。
4. 创建完成后，在应用详情页能看到 **API Key** 和 **Secret Key**，复制到 `.env`：
   ```dotenv
   OCR_ENABLED=true
   BAIDU_OCR_API_KEY=你的_API_KEY
   BAIDU_OCR_SECRET_KEY=你的_SECRET_KEY
   OCR_ENGINE=accurate_basic   # 或 general_basic（标准版免费额度更高）
   ```
5. 免费额度：标准版每月 1000 次免费、高精度版 500 次（以百度官网最新政策为准）。

### 修改日期筛选（可选）

启用后，只处理 `mtime` 在指定日期范围内的文件：

```dotenv
MTIME_FILTER_ENABLED=true
MTIME_AFTER=2024-01-01           # 留空表示不限
MTIME_BEFORE=2024-12-31 23:59:59 # 留空表示不限
```

支持 `YYYY-MM-DD` 与 `YYYY-MM-DD HH:MM:SS` 格式，区间为闭区间。被排除的文件会
出现在 `conversion_report.md` 的「⏰ 修改日期过滤」章节，方便复核。

CLI 可临时覆盖：`--filter-mtime` / `--no-filter-mtime`。

### 日志类文件单文件截取（仅影响 `.log` `.tlog` `.rlog`）

```dotenv
LOG_FILE_MAX_BYTES=10485760      # 10 MiB；按字节算，超过的部分按"头+尾"截取
LOG_TRUNCATE_HEAD_RATIO=0.5      # 头部占比，剩下给尾部
```

截取算法：

- 字节级切分，**严格落在 UTF-8 字符边界**，绝不破坏多字节字符
- 优先选行边界（`\n`），中间用 `... TRUNCATED N bytes ...` 标记取代
- `.tlog` 走 pymavlink 后，输出的 markdown 文本若仍超限额，会再做一次同款截取

### 中间过程文件保留

```dotenv
KEEP_INTERMEDIATE_FILES=true
INTERMEDIATE_SUBDIR=intermediate
```

启用后，每个文件的转换结果会单独写到 `<output>/intermediate/`：

- `<file_id>__<safe_name>.md` —— 人可读，含元信息头 + 完整 markdown
- `<file_id>__<safe_name>.extra.json` —— 机器友好，含 status / warnings / extra
- `INDEX.md` —— 一张总表，列出所有源文件 → 中间产物的对应关系

CLI 可临时覆盖：`--keep-intermediate` / `--no-keep-intermediate`。

---

### 通用文件截取（`TRUNC_*`，所有类型）

**场景**：要把一万个大文件喂给 LLM，每个文件都想砍到「头 xMB + 中间随机抽 N 个 xMB 块 + 尾 xMB」。

**核心配置**：

```dotenv
# 全局开关
TRUNC_ENABLED=true
TRUNC_SEED=0        # 随机种子；同种子+同文件名 ⇒ 结果可复现

# 默认（未列出的类型走这里）
TRUNC_DEFAULT_ENABLED=false
TRUNC_DEFAULT_HEAD_MB=2
TRUNC_DEFAULT_TAIL_MB=2
TRUNC_DEFAULT_CHUNK_MB=1
TRUNC_DEFAULT_MIDDLE_CHUNKS=2

# 按 kind（pdf/docx/xlsx/wps/markdown/html/image/json/csv/param/txt/log/tlog/rlog）
TRUNC_KIND_LOG_ENABLED=true
TRUNC_KIND_LOG_HEAD_MB=2
TRUNC_KIND_LOG_TAIL_MB=2
TRUNC_KIND_LOG_CHUNK_MB=1
TRUNC_KIND_LOG_MIDDLE_CHUNKS=2

# 按扩展名（最高优先级，可在同 kind 下精调）
TRUNC_EXT_LOG_ENABLED=true
TRUNC_EXT_LOG_HEAD_MB=2
TRUNC_EXT_LOG_TAIL_MB=2
TRUNC_EXT_LOG_CHUNK_MB=1
TRUNC_EXT_LOG_MIDDLE_CHUNKS=2
```

**优先级**：扩展名 `.xxx` > kind > default。

**`x` 的含义**：`HEAD_MB` / `TAIL_MB` / `CHUNK_MB` 支持 **0 / 小数 / 整数**。
`0` 表示「该部分不取」。例如 `HEAD_MB=0, TAIL_MB=0, CHUNK_MB=0.5, MIDDLE_CHUNKS=3`
表示「不取头尾，只抽 3 个 0.5MB 中间块」。

**作用层**：

- **pre-read**（仅 `txt/csv/json/md/html/log/rlog/param`）：在 `read_bytes()` 后立即截取，
  避免后续 pandas/json 解析对超大文件做无意义工作。
- **post-convert**（所有类型）：作用在已生成的 markdown 字节上，作为最终兜底。

**输出标记**：截取产物中会带 `--- SAMPLE i/N [offset ...] ---` 与
`--- TAIL [...] ---` 标签，让 LLM 知道这是采样、不是连续内容。

**报告**：`conversion_report.md` 中的「✂️ 通用截取」章节列出每个被截取文件的
原始/保留字节数、各部分占比、采样块数。

> 旧的 `LOG_FILE_MAX_BYTES` / `LOG_TRUNCATE_HEAD_RATIO` 仍生效（仅作 `log/tlog/rlog`
> 的 post-convert 兜底）；如果你只用 `TRUNC_*` 体系，可忽略它们。

---

### 按文件类型的大小筛选（`SIZE_FILTER_*`）

**场景**:扫描阶段就把过大 / 过小的文件直接排除，根本不进入转换流水线。

```dotenv
SIZE_FILTER_ENABLED=true

# 默认（未列出的类型走这里）；-1 或 false 表示关闭
SIZE_FILTER_DEFAULT_MIN_MB=-1
SIZE_FILTER_DEFAULT_MAX_MB=-1

# 按 kind
SIZE_FILTER_KIND_LOG_MIN_MB=1
SIZE_FILTER_KIND_LOG_MAX_MB=50
SIZE_FILTER_KIND_IMAGE_MAX_MB=4

# 按扩展名（最高优先级）
SIZE_FILTER_EXT_PNG_MIN_MB=0.1
SIZE_FILTER_EXT_PNG_MAX_MB=4
```

- 数值单位：**MB**，支持 0 / 小数 / 整数。
- `-1` / `false` / `off` / `no` / `none` / 空值 ⇒ **该项筛选关闭**。
- `MIN_MB=0` 也视为「不限下限」（语义化更直观）。
- 区间为闭区间：`min ≤ size ≤ max`。
- 被过滤的文件会出现在 `conversion_report.md` 的「📏 按大小过滤」章节。

---

### 创建日期筛选（`CTIME_*`）

与修改日期筛选语义一致，但使用文件「创建时间」做筛选。

```dotenv
CTIME_FILTER_ENABLED=true
CTIME_AFTER=2024-01-01
CTIME_BEFORE=2024-12-31 23:59:59
```

平台行为参考前文「文件元信息附注」节中的 ctime 语义表。
ctime 不可用的文件**默认放行**（避免老 Linux 误杀）。

---

### 进目录扩展名 / kind 筛选（`ELIGIBILITY_*`）

在「全量枚举」与「提取流水线」之间应用，决定哪些文件有资格进入提取。

```dotenv
# 白名单：只接受 Python 代码 + Markdown
ELIGIBILITY_INCLUDE_KINDS=code_py,markdown

# 黑名单：拒绝所有日志类
ELIGIBILITY_EXCLUDE_KINDS=log,tlog,rlog

# 用扩展名拒绝某几种
ELIGIBILITY_EXCLUDE_EXTS=.tlog,.rlog

# 对未识别扩展名直接拒，不进流水线占位
ELIGIBILITY_UNSUPPORTED_POLICY=reject    # allow（默认）/ reject
```

- 多个值用逗号分隔；任一为空 = 不限制。
- **blacklist 优先于 whitelist**：在 exclude 中的即便也在 include 也会被拒。
- 不传 `INCLUDE_*` 时默认放行所有 kind / ext，只走 blacklist。

---

### 完整文件目录（全量枚举清单）

每次运行都生成一份**不受任何筛选影响**的完整目录，包括：

- **每卷 `merged_part_*.md` 第 1 卷顶部**：人类可读表格 + 机器可读 TSV
- **`conversion_report.md` 末尾**：同上一份

清单中每个文件都标注：

- `size_bytes`：**精确字节数**（不四舍五入；超过 10MB 的文件几乎是唯一指纹）
- `size_class`：数量级标签（`<1KB` / `1KB-1MB` / `1MB-10MB` / `10MB-100MB` / `>100MB`）
- `mtime` / `ctime` / `ctime_source`
- ✅/❌ 资格 + 拒因（如被进目录筛选拒绝）

```dotenv
FULL_INVENTORY_IN_MERGED=true   # 写到每卷 merged_part_*.md 第 1 卷顶部
FULL_INVENTORY_IN_REPORT=true   # 写到 conversion_report.md
```

**为什么这么设计**：让 LLM 一眼看到「这次扫描有多少文件、谁进了谁没进」，
并能通过 `(rel_path, size_bytes)` 组合做文件级查重——这正是处理上万文件时的核心需求。

---

### 默认配置文件加载顺序

仓库自带 `.env.default`，集中维护所有默认值；用户在 `.env` 中只写需要改的项即可。

加载顺序：

1. 先加载用户的 `.env`（如存在，CLI `--env` 可指定路径）
2. 再加载 `.env.default` 做兜底（**不覆盖**用户已设置的项）

也就是说：**用户 `.env` 中显式写了的项 → 用用户的；没写的项 → 用 `.env.default` 的**。
所以你的 `.env` 通常只放需要个性化的那几行，剩下的让 `.env.default` 兜底。

---

### 文件元信息附注（哈希 / 修改日期 / 创建日期 / 大小）

```dotenv
FILE_METADATA_ENABLED=true
FILE_HASH_ALGO=sha256              # sha256 / sha1 / md5
FILE_METADATA_POSITION=both        # toc / body / both / none
FILE_METADATA_SHOW_HASH=true
FILE_METADATA_SHOW_MTIME=true
FILE_METADATA_SHOW_CTIME=true
FILE_METADATA_SHOW_SIZE=true
FILE_HASH_MAX_BYTES=0              # 0 = 不限；>0 时大于此值的文件不计算哈希
FILE_HASH_SHORT_LEN=16             # TOC 紧凑显示的哈希前缀长度（4-64）
```

启用后，每个文件的元信息会按位置追加在合并 markdown 中：

- **`toc`**：仅在目录条目末尾以紧凑串显示，例：
  ```
  1. [docs/report.pdf](#file-abc12345) — PDF — ✅成功 — `1.2 MB · m:2025-03-15 · b:2025-03-10 · sha256:a1b2c3d4e5f6789a`
  ```
- **`body`**：仅在每个文件正文头（FILE START 块）以多行 verbose 形式显示
- **`both`**：两处都加（最适合给 LLM 用，方便扫读 + 精确引用）
- **`none`**：不显示（即便 enabled=true；常用于"只算 hash 给查重用，不污染输出"）

**关于"创建时间"的语义说明**：

| 平台 | 实际语义 | 标记 |
| --- | --- | --- |
| macOS、新版 Linux（statx）、BSD | 真创建时间 (`st_birthtime`) | `创建时间` 或 TOC 中的 `b:` |
| Windows | `st_ctime` 即为创建时间 | `创建时间` 或 TOC 中的 `b:` |
| 老版 Linux / 部分 ext4 | 退化为 `st_ctime`（元数据变更时间，**不是真创建**） | `创建时间(*近似)` 或 TOC 中的 `*c:` |
| 文件系统不提供 | 标注「不可用」 | — |

CLI 可临时覆盖：`--with-metadata` / `--no-metadata`，以及 `--metadata-position {toc,body,both,none}`。

---

### 重复文件检测（AI 友好查重报告）

```dotenv
DUPLICATE_DETECTION_ENABLED=true
```

启用后基于 `FILE_HASH_ALGO` 计算的内容哈希查找重复文件。开启此项会**自动启用**
`FILE_METADATA_ENABLED`（即便 `.env` 没显式开），因为查重必须先算 hash。

每卷 `merged_part_*.md` 顶部的目录之后会插入一段 **AI 友好查重报告**，包含：

1. **AI 阅读说明**：明确告诉 LLM 这块是干嘛的、怎么把重复文件视为同一份内容
2. **统计行**：组数、涉及文件数、冗余字节数
3. **机器可读索引**：紧凑的 TSV 块，列出 `<group_idx>\t<role>\t<file_id>\t<size>\t<rel_path>`
4. **每组详情**：完整哈希、主副本/副本路径、对应 `file_id`

主副本（`[MAIN]`）选取规则：组内**相对路径字典序最靠前**的文件。

`conversion_report.md` 也会增加「🔁 重复文件检测摘要」章节，列出每组的主副本与副本数。

CLI：`--dedup` / `--no-dedup`。

> 边界场景：如果用户显式 `--no-metadata --dedup`，则查重照样跑，但 TOC/正文不显示
> 任何元信息（自动把 `position` 拉成 `none`）—— 只保留最终的查重报告。

---

## 使用

### 最小可跑命令

```bash
python convert.py --input ./my_docs --output ./out
```

或者**在 `.env` 中预设默认路径**后直接运行：

```dotenv
# .env
INPUT_DIR=./my_docs
OUTPUT_DIR=./out
```

```bash
python convert.py
```

CLI 与 `.env` 同时给出时，CLI 优先。两者都没给会以退出码 2 报错并提示。

### 完整参数

```bash
python convert.py \
    --input  ./my_docs                  \
    --output ./out                      \  # 默认会自动加 _YYYYMMDD_HHMMSS
    --max-size-mb 19                    \  # 单卷上限，覆盖 .env
    --verbose                           \  # DEBUG 级日志
    --no-ocr                            \  # 临时关闭 OCR（图片全跳过）
    --filter-mtime                      \  # 启用 mtime 筛选（日期来自 .env）
    --keep-intermediate                 \  # 保留中间产物
    --with-metadata                     \  # 元信息附注（hash/mtime/ctime/size）
    --metadata-position both            \  # toc / body / both / none
    --dedup                             \  # 启用查重 + AI 友好报告
    --no-timestamp-output               \  # 禁用输出目录时间戳后缀
    --env ./.env                           # 指定 .env 路径
```

互斥开关都支持反向：`--no-filter-mtime` / `--no-keep-intermediate` /
`--no-metadata` / `--no-dedup` / `--no-timestamp-output`。
**配置优先级**：`CLI 参数 > .env > 代码默认值`。

> 💡 **输出目录时间戳**：默认 `--output ./out` 会实际写到 `./out_20260524_223330`，
> 这样多次运行不会互相覆盖。要关闭：CLI 加 `--no-timestamp-output`，
> 或在 `.env` 中设 `OUTPUT_TIMESTAMP_SUFFIX=false`。

### 输出文件

```
out/
├── merged_part_001_of_005.md   # 第 1 卷
├── merged_part_002_of_005.md   # 第 2 卷
├── ...
├── merged_part_005_of_005.md   # 末卷，含 END OF BUNDLE
├── conversion_report.md        # 转换报告（含 mtime 过滤、日志截断章节）
├── run.log                     # 完整运行日志（DEBUG 级）
└── intermediate/               # 仅 KEEP_INTERMEDIATE_FILES=true 时存在
    ├── INDEX.md
    ├── <id>__<safe_name>.md
    └── <id>__<safe_name>.extra.json
```

---

## 中断、恢复、与部分导出（v5.1 新增）

### Ctrl+C 中断（暂停当前任务）

转换运行中按 **Ctrl+C** 触发"软暂停"：

1. 第一次 Ctrl+C：当前正在转换的文件会先收尾（不会立刻把进程砍掉），然后退出。
   - 已转换的所有文件块都已落盘到 `<output>/.spool/` 与 `<output>/.pause/` 状态目录。
   - 工具会尝试把"截至此刻的报告"写到 `<output>/partial_export/`。
   - 默认 1 分钟内会结束 Python 进程。
2. 如果某个超大文件正在转、1 分钟还没释放：**再按一次 Ctrl+C** —— 工具会直接
   `os._exit(130)` 强制退出。即使强退，`.spool/` 和 `.pause/` 都不会丢，下次还能恢复。

### 继续上次任务

```
python convert.py --resume ./out
```

- 必须传 `--resume <output_dir>`（注意是输出目录，不是输入目录）。
- 启动时会读 `<output>/.pause/session.json`，打印上次进度并询问 `[Y/n]`。
  脚本批处理可加 `--no-resume-prompt` 自动确认。
- **不会重新扫盘**：使用上次扫描结果 `scan_state.jsonl`，保证文件 ID 一致。
- **只转剩余 pending 文件**：已完成的从 spool 中复用，不重复消耗 OCR 配额。
- 全部完成后会清掉 `.pause/`，写最终报告到 `<output>/conversion_report.md`。

### 只导出当前缓存中的部分报告（不继续转换）

万一原始输入目录已经搬走、不打算 resume，只想拿到"已转换部分"的合并 MD：

```
python export_partial_report.py --output ./out
```

- 读 `<output>/.pause/` + `<output>/.spool/`，写到 `<output>/partial_export/`。
- **不动 `.spool/` 和 `.pause/`** —— 之后还能 `--resume` 继续转。
- 没有 `.pause/` 时可加 `--from-spool-only` 兜底，纯按 spool 文件拼。

### .pause/ 目录结构（调试用）

```
out/.pause/
├── session.json         # 运行元数据 + 状态: running / interrupted / completed
├── scan_state.jsonl     # 扫描快照：每行一个 ScannedFile (含 file_id, hash, eligible)
└── entries.jsonl        # 转换日志：每行一条已完成记录 (status, warnings, spool_path...)
```

正常完成后 `.pause/` 会自动删除。

---



1. **图片型 PDF 不自动 OCR**：检测到后只发警告 + 在报告中列出。两条扩展路线写在
   `<故障排查>` 节末。
2. **CSV/XLSX 行数截断**：超过 `CSV_MAX_ROWS`（默认 200）会截断；这是
   为了控制单卷体积。需要全量请把这个值调大或预先把数据切片。
3. **token 缓存**：`OCR_TOKEN_CACHE` 默认放在工作目录的 `.baidu_token_cache.json`，
   有效期 30 天。换 API key 时缓存会自动失效。
4. **限流**：`OCR_MIN_INTERVAL=0.5` 表示两次 OCR 调用至少间隔 0.5 秒，
   规避百度 QPS 限制（高精度版默认 QPS=2）。
5. **大单文件**：如果某个文件转换出的 markdown 超过 `MAX_SIZE_MB`，
   会在行边界处中间切，前后插入 `PART BREAK` / `CONTINUED` 标记。
6. **`.tlog` 解析消息上限**：单文件最多解析 200,000 条消息，避免大型 tlog
   把内存撑爆。被截断时报告里会有提示。
7. **`.wps` 路径选择**：探测到 OOXML 头（`PK\x03\x04`）→ 直接 mammoth；探测到 OLE 头
   （`D0 CF 11 E0`）→ 找 LibreOffice → 找不到就 olefile 兜底。三条都不行才报错占位。
8. **`.rlog` 二进制识别**：默认抽前 4KB 看 NUL 字节 + 可打印率，二进制 → 走 hex 预览。
   如果你确知是文本，把扩展名改为 `.log` 也可以；行为基本一致。
9. **失败兜底**：单文件失败不会中断；占位仍然在合并 MD 中、并在报告里
   带异常信息（OCR 失败还会带百度错误码）。
10. **编码**：所有产物统一 UTF-8。

---

## 项目结构

```
project/
├── convert.py              # CLI 入口
├── config.py               # .env 加载 + AppConfig dataclass
├── scanner.py              # 扫描文件夹、识别类型、mtime 过滤
├── converters/             # 各类文件 → markdown
│   ├── base.py
│   ├── _log_utils.py       # 日志类文件的"头+尾"截取（UTF-8 安全）
│   ├── pdf_converter.py
│   ├── docx_converter.py
│   ├── txt_converter.py
│   ├── csv_converter.py
│   ├── xlsx_converter.py
│   ├── json_converter.py
│   ├── md_converter.py
│   ├── html_converter.py
│   ├── image_converter.py
│   ├── param_converter.py  # 新：MissionPlanner .param
│   ├── log_converter.py    # 新：通用文本日志
│   ├── rlog_converter.py   # 新：自适应文本/二进制
│   ├── tlog_converter.py   # 新：MAVLink，可选 pymavlink
│   ├── wps_converter.py    # 新：金山 WPS
│   └── code_converter.py   # 新：代码 / 配置类（.py .sh .yaml 等 26 种）
├── ocr/
│   └── baidu.py            # 百度 OCR 客户端 + token 缓存
├── merger.py               # 合并 + TOC + FILE START/END 分隔符（含元信息附注）
├── splitter.py             # 分卷 + 卷头卷尾 + PART BREAK
├── hasher.py               # 新：按需流式计算文件内容哈希（sha256/sha1/md5）
├── dedup.py                # 新：查重 + AI 友好查重报告渲染
├── reporter.py             # 生成 conversion_report.md
├── truncation.py           # 新：通用截取引擎（头+尾+中间随机块）
├── size_filter.py          # 新：按文件类型的大小筛选
├── ext_filter.py           # 新：扩展名 / kind 白/黑名单（进目录筛选）
├── .env.default            # 新：默认配置（被 .env 覆盖；与代码一起提交）
├── .env.example            # 配置模板（请复制为 .env）
├── .gitignore
├── requirements.txt
├── requirements-optional.txt
└── README.md
```

---

## 故障排查

| 现象 | 排查 |
| --- | --- |
| `[配置错误] OCR_ENABLED=true 但 ... 未配置` | 在 `.env` 填 API key/Secret，或把 `OCR_ENABLED=false` |
| `[配置错误] MTIME_AFTER=... 晚于 MTIME_BEFORE=...` | 检查 `.env` 里的两个日期顺序 |
| `[配置错误] MTIME_AFTER=... 无法解析为日期` | 用 `YYYY-MM-DD` 或 `YYYY-MM-DD HH:MM:SS` 格式 |
| 启动报 `获取 access_token 失败` | 检查 API key 是否正确、网络是否能访问 `aip.baidubce.com` |
| OCR 调用错误码 17/18/19 | 限流/配额耗尽：调大 `OCR_MIN_INTERVAL`、买更多额度，或换 `general_basic` |
| 某个 PDF 提示「图片型 PDF」 | 真的是扫描件；用 `tesseract` + `ocrmypdf` 预处理后再跑 |
| `pip install` 在 ARM 上失败 | `sudo apt install build-essential python3-dev libffi-dev` 后重试 |
| Excel 报 `Bad zip file` | 文件可能是 .xls（旧版 OLE 格式）。本工具只支持 .xlsx，请先另存为 .xlsx |
| `.tlog` 输出"未启用 pymavlink" | `pip install pymavlink` 然后重跑 |
| `.wps` 全部转换失败 | 用 WPS Office 另存为 .docx 后重跑；或装 LibreOffice 让 OLE 路径生效 |

### 让图片型 PDF 走 OCR 的两条路线

- **方案 A：本地 Tesseract**（不需要联网、免费）
  - Windows: 装 https://github.com/UB-Mannheim/tesseract/wiki，添加到 PATH
  - 统信 UOS: `sudo apt install tesseract-ocr tesseract-ocr-chi-sim`
  - 用 `ocrmypdf` 包预处理：`pip install ocrmypdf` →
    `ocrmypdf -l chi_sim+eng input.pdf output.pdf`
  - 然后把 `output.pdf` 喂给本工具即可（提取出的就是文字）。
- **方案 B：把每页渲染成图片走百度 OCR**（本工具未实现，可自行扩展）
  - 用 `PyMuPDF (fitz)` 把每页 `page.get_pixmap()` 转 PNG
  - 调 `BaiduOCRClient.recognize_image()` 即可
