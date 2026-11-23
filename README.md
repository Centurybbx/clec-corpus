# clec-corpus

CLEC 语料清洗 ETL（Stage 1）。本仓库把只读原始采集目录
`/Users/century/Vibing/clec/collection/` 转换为版本化、可审计的标准数据包：

- `dist/corpus.jsonl`：每行一个可检索段落（contract v2）
- `dist/meta.json`：`clec stats` 直接读取的统计与已知缺口
- `dist/etl-audit.md`：独立质量审计报告
- `dist/doc-manifest.jsonl`：逐文档段落产出清单（审计 `docs_with_zero_passages`）
- `config/synonyms.json`：60 组高确定性金融/两岸/ASR 同义词
- `config/source_policy.json`：9 类正文来源的开关与合规策略

原始语料、清洗产物和压缩包均不入 Git。原始目录始终以只读方式打开。

## 目录结构

```text
clec-corpus/
├── .gitignore                 # dist/、raw/、*.jsonl、*.tar.zst、缓存
├── pyproject.toml             # pytest pythonpath 配置
├── README.md
├── config/
│   ├── synonyms.json          # 30–60 组同义词
│   └── source_policy.json     # 来源开关；合规来源默认 full
├── etl/
│   ├── clean.py               # 网页模板强剥离、标题/日期规范化
│   ├── dedup.py               # work_id 对齐、5-gram MinHash、含括率去重
│   ├── hallucination.py       # Whisper 幻觉门禁
│   ├── chunk.py               # 400–600 字自然语义切段 + 时间戳
│   ├── schema.py              # contract v2 校验
│   ├── build_corpus.py        # CLI 总入口
│   └── audit_corpus.py        # 独立审计
└── tests/                     # 清洗、去重、幻觉、切段、契约、回归测试
```

## 复现命令

```bash
cd /Users/century/Vibing/clec-corpus

# 1) 全量清洗（只读 collection，输出到 gitignored 的 dist/）
python3 -m etl.build_corpus \
  --source-dir /Users/century/Vibing/clec/collection \
  --out-dir dist

# 2) 独立审计（扫描全量 corpus + meta，写出 dist/etl-audit.md）
python3 -m etl.audit_corpus \
  --corpus dist/corpus.jsonl \
  --meta dist/meta.json \
  --out dist/etl-audit.md

# 3) 单元测试
python3 -m pytest -q
```

如果本机没有 pytest：

```bash
python3 -m venv .venv
.venv/bin/pip install pytest
.venv/bin/python -m pytest -q
```

## 跨仓库契约（v2）

`corpus.jsonl` 每行字段：

```json
{
  "doc_id": "p_ep-00001_tr_ab12cd34_0000",
  "work_id": "ep-00001",
  "source_id": "drive_srt_00001",
  "ordinal": 0,
  "kind": "transcript",
  "quality": 2,
  "date": "2024-06-15",
  "start_sec": 0,
  "end_sec": 30,
  "title": "【00001】投資通訊 (1)2017 十二月",
  "heading": "",
  "body": "……",
  "origin_url": "https://clectw.com/ep/ep-00001",
  "other_kinds": ["summary"],
  "license_mode": "full"
}
```

硬约束：

- `doc_id` 全局唯一非空；`work_id`、`source_id`、`title`、`body` 非空 UTF-8；
- `ordinal` 在同一 `source_id` 内从 `0` 严格连续；
- `kind ∈ {doc, transcript, qa, summary}`；
- `quality ∈ {1,2,3,4}`；`4` 表示 Whisper 幻觉可疑段落；
- `date` 为严格合法的 `YYYY-MM-DD` 或 `null`；
- `start_sec/end_sec` 为可选非负整数；
- `license_mode` 全量 `full`；
- 不进行破坏性繁简转换（繁简/两岸词归一由 Rust 引擎 `cjk_norm` 负责）。

## 来源策略

| 来源 | 处理 | kind / quality |
| --- | --- | --- |
| `sources/drive/text/` | 剥捕获头/来源引用/URL；同 kind 去重 | `doc` / `1` |
| `text/local/` | 讲义正常清洗；6 份表格只保留表头+关键汇总 | `doc` / `1` |
| `sources/clec-md/knowledge/` | 清 frontmatter/导航链接；qa 分类为问答 | `qa`/`2` 或 `summary`/`3` |
| `sources/clec-md/posts/` | 博客正文 | `doc` / `1` |
| `sources/drive/archive-transcripts/` | 每组只取 SRT；TXT/MD 全部丢弃 | `transcript` / `2` |
| `web/clectw/transcripts/` | 无 Drive SRT 时保留 | `transcript` / `2` |
| `web/clectw/text/` | 在 AI 免责声明处切开，只取摘要，丢弃页内嵌逐字稿 | `summary` / `3` |
| `transcripts/local-asr/`、`podcast-asr/` | 经幻觉门禁 | `transcript` / `2` |
| `web/cfnavi/` | 剥导航/标签/推荐区 | `summary` / `3` |
| `web/clec168/` 博客、书籍、简报 | 剥页脚/分页；纯视频介绍页不读 | `doc` / `1` |
| `sources/podcast/` | 仅补日期/链接元数据，不入正文 | — |
| `transcripts/quarantine/` | 永久剔除 | — |

`ep-00176`、`ep-00260`、`ep-00295` 是长视频 `ep-` 系列中被判定为严重 Whisper 幻觉的三集，整部作品（含摘要、Drive SRT、cfnavi 对应页）全部跳过，并写入
`meta.json.known_gaps`。注意 `short-00176`、`short-00260`、`short-00295` 是编号相同但内容完全不同的短篇，属于正常作品。

## 实地探查后对规格假设的修正

这些修正是基于 `collection/` 真实文件得出结论，不是猜测：

1. **Drive archive 的 5 位数字不是集号。** `sources/drive/archive-transcripts/00001-01.srt`
   的组号是上传槽位；同组 MD 头部可能写着 `00012`、`00176` 甚至 `00566`。
   ETL 先解析 MD 头部，再用 5-gram containment（≥ 0.45）与 clectw 逐字稿/摘要做内容对齐，
   并校验时长差不超过 30%；置信度不足时保留为 `drive-<group>`，绝不按槽位号硬绑集号，
   也绝不覆盖原始 clectw 逐字稿。
2. **clectw `text/ep/` 页面是“摘要 + 页内嵌逐字稿”的混合页。**
   2156 页中 722 页有 `本週課程概述`，1431 页只有摘要后直接接逐字稿，3 页是隔离集摘要。
   ETL 在 `※ 本週報由 AI…` / `※ 逐字稿由影片語音…` 处切开，`kind=summary` 只取摘要，
   页内逐字稿不再重复入库。
3. **clectw 编号跨系列复用。** `ep-00001`（长视频）与 `short-00001`（短篇）、
   `talk-00012`（漫谈）、`origin-00012`（缘起今日）是不同作品；`work_id` 必须保留系列前缀。
4. **Drive archive 有 1 个 `.str` 拼写错误文件（`00082-01.str`）和 2 组空文件。**
   空 SRT/TXT 组跳过；`.str` 按 SRT 解析。
5. **clec168 的 2,153 个视频介绍页不进入正文。** 博客、书籍章节和简报页正常入库；
   与 Drive PDF 内容重复的页面由 MinHash 合并，保留最完整版本。
6. **电子表格不能按单元格入库。** `text/local` 的 6 份 `.xlsx` 文本转储只保留
   工作表名、情景参数（30% / 50%）、表头、关键数值汇总行；单元格坐标
   （`B11:` / `C12:`）与本机绝对路径全部剥离。
7. **多版本书籍只保留最新/最完整版本。** `The-Wisdom-of-CLEC_EN`、
   `CLEC宝典_SC`、`CLEC寶典_ZH`、`理財聖經CLEC版` 等按“归一化书名 + 语言/文字”
   分组，保留版本号最高者（同版本再取正文最长者），避免 V1.5.1 与 V1.14.2
   同时入库造成大量重复。切段后还会对跨 `work_id` 完全相同的 `body` 做精确去重。

统计口径提示：`clec168_blog`、`clec168_presentations` 的页面数远大于最终
`source_type` 文档数，因为其中大量页面与 Drive PDF 是同一篇内容，MinHash
合并后保留最完整版本；`clectw_transcripts` 同理，Drive SRT 优先时对应
clectw 纯文本逐字稿会被同作品同类型合并。正文内容没有丢失，只是归并到了
更完整的 `source_id` 下。

## 最近一次真实验收（全量）

- 原始文件读取：7,816 个；原始字符：120,708,879
- 去重前文档：7,771；去重后：5,367；段落：50,646
- 作品数：2,817；来源数：5,359
- 类型：`doc` 11,698 / `transcript` 27,955 / `qa` 419 / `summary` 10,574
- 质量：q1 11,698 / q2 28,268 / q3 10,574 / q4 106
- 模板正则命中：95 / 50,646 = 0.1876%（阈值 < 0.5%；含制式页尾/播客/社交链接）
- `ep-00176/00260/00295` 泄露：0；真实 Drive 槽位 `00269/00221/00300`：0；内容指纹：0；`known_gaps` 完整
- `ep-02xxx` 伪年份作品：0（修复前 10 个作品 / 2,978 段）
- `date == 2026-11-06` 采集日污染：0（修复前 1,918 段 / 87 作品）
- Drive 同组 TXT/SRT/MD 重复：0
- 同源 `(start_sec, end_sec)` 重复：0；同源时间戳重叠：0（修复前 5,461 段重复区间）
- 跨 `work_id` 完全相同 `body`：0
- `docs_with_zero_passages`：0 / 5,367
- 同 `work_id` 同时含 `transcript` + `summary`：2,148（阈值 ≥ 2,000）
- clec168 纯视频介绍页入正文：0
- 100–900 字段落：50,643 / 50,646 = 99.99%（阈值 ≥ 95%）
- 多版本书籍淘汰：17 个旧/重复版本；跨 work 完全重复段落删除：950
- 全量 `license_mode=full`
- 单元测试：58 passed

完整报告见 `dist/etl-audit.md`（不提交 Git）。

## 数据源与合规

- 原始语料仅本地只读使用，不复制进本仓库。
- 第三方来源按项目负责人确认的策略默认 `license_mode=full`。
- 所有自动逐字稿与网页摘要均未经逐字人工校对；关键数字与观点应回溯原片。
- 本知识库为非官方学习检索工具，不构成投资建议。
