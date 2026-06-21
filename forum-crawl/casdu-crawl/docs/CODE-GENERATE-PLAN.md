# bbs.casdu.cn 论坛爬虫 — 实现计划（修订版）

## Context

为山东大学自行车协会论坛（bbs.casdu.cn）编写全量爬虫，将帖子数据抓取、分类、标签化后本地存储，支持后续增量更新。输出到 `D:\ziYuan\github\casdu`。

**核心约束**：
- 仅抓取公开可见的论坛页面（archiver + forum.php），不访问后台/数据库
- 仅记录用户名等基本公开信息，不提取真实姓名/手机号/邮箱等隐私数据
- 区分置顶帖与普通帖，全局置顶帖不重复索引

---

## 站点实测结论（自检修正）

| 项目 | 实测值 | 计划原值（错误） |
|------|--------|-----------------|
| 系统 | Discuz! X3.2 + nginx 反代 | ✅ 正确 |
| 编码 | GBK，需 `errors='replace'`（部分页有非法字节如 0x80） | ⚠️ 原计划未提及容错 |
| 版块总数 | **38** 个 | ~~45~~ |
| Archiver 翻页格式 | `?fid-X.html&page=N` / `?tid-X.html&page=N` | ~~`-page-N.html`~~ ❌ |
| 每页线程数 | 32 | ✅ 正确 |
| 每页帖子数 | **15**（archiver 固定 15 帖/页） | 实测 tid=8654: 15+15+7=37帖共3页 |
| robots.txt | `/archiver/` 和 `/forum.php` 均 **允许** | ✅ 确认 |
| 限速头 | 无 X-RateLimit 头 | ✅ |
| 隐藏版块 | fid=115 不在 archiver 首页但可直接访问 | 新发现 |
| 长帖分页 | tid=8654 共 3 页（archiver `&page=N`） | ✅ 已验证 |
| 编码 | **GBK 解码正常**（终端乱码是渲染问题，写入文件后中文完整可读） | ✅ 验证通过 |
| 登录墙 | **未检测到登录可见版块**，全部 38 版块匿名可访问 | 新发现 |
| 登录机制 | Discuz! X3.2 登录需 formhash CSRF token，简单 POST 会返回 System Error | 新发现（已降级为匿名模式） |
| Archiver 标签 | 作者块为 `<p class="author">` 非 `<div>`（Discuz! X3.2 标准） | 新发现（已修复） |
| 版块链接去重 | 每个版块有图标链接（无文字）+ 文字链接，需先过滤空 title 再去重 | 新发现（已修复） |
| fid=152 实测 | 8 主题 135 帖，16s 完成，数据质量验证通过 | 测试通过 |

---

## 隐私与合规原则

### 1. 仅抓取公开信息

- 所有数据来源仅为 bbs.casdu.cn **公开可访问**的页面（`archiver/` 纯文本归档）
- **不访问**论坛后台、数据库、管理界面、或任何需登录才能查看的页面
- **不尝试** SQL 注入、密码爆破、越权访问等任何攻击性行为

### 2. 用户信息最小化

只记录以下公开可见的论坛身份信息：
- **用户名**（论坛显示名，非真实姓名）
- **发帖时间**
- **帖子正文**（用户主动公开发布的内容）

**明确不抓取**：
- 真实姓名（即使用户在正文中自曝，也不做结构化提取）
- 手机号、邮箱、QQ 号、微信号（同上，不做结构化提取）
- 住址、身份证号、学号
- IP 地址、User-Agent 等 HTTP 元信息
- 头像图片、附件文件

### 3. 帖子正文中的敏感信息处理

帖子正文作为公开内容整体保留（用户已主动公开发布），但 `classifier.py` 的标签引擎**不将**隐私信息（手机号/邮箱/姓名等）作为标签结构化提取。标签仅限路线、事件类型、角色、问题类型四类公开议题。

---

## 置顶帖与普通帖区分

Discuz! 论坛中，全局置顶帖会出现在**所有版块**的列表顶部，版块置顶帖仅在该版块顶部显示。

### 识别方式

Archiver 版块列表页中，置顶帖和普通帖都在同一 `<li>` 列表中，但主站 `forumdisplay` 页可通过 CSS 类名区分：
- `class="icn_pt"` → 全局置顶
- `class="icn_pt topic"` → 版块置顶
- 无特殊 class → 普通帖

### 处理策略

由于我们使用 archiver 入口（无 CSS 类名），采用以下策略：

1. **全局置顶帖检测**：同一 tid 在 ≥3 个不同 fid 版块列表中出现 → 标记为全局置顶
2. **版块置顶帖检测**：tid 仅在 1 个版块中出现，但在 `forumdisplay` 中位于"置顶分隔线"之上 → 标记为版块置顶
3. **存储标记**：在 `threads` 表中增加 `sticky` 字段：
   - `0` = 普通帖
   - `1` = 版块置顶
   - `2` = 全局置顶
4. **索引时去重**：全局置顶帖在每个版块只保留首次出现的版块归属，后续版块中出现的同一个 tid 直接跳过

### 实现位置

- `utils.py` 中增加 `is_sticky_thread(html, tid)` 函数（解析主站 `forumdisplay` 页）
- `scraper.py` Phase 2（收集线程列表）中，记录 `(tid, fid, title, is_sticky)` 四元组
- `storage.py` 的 `threads` 表中增加 `sticky INTEGER DEFAULT 0` 字段

---

## 文件结构

```
D:\ziYuan\github\casdu\
├── scraper.py              # 主控：全量 + 增量流程
├── config.py               # 38版块字典 + 标签词表 + 速率常量
├── utils.py                # HTTP请求、GBK容错解码、HTML清洗、页面解析
├── classifier.py           # 自动分类标签引擎
├── storage.py              # JSONL + SQLite + checkpoint
├── data/
│   ├── threads.jsonl       # 所有帖子（每行 = 一个 post）
│   ├── index.db            # SQLite（threads + posts 双表 + FTS5）
│   └── checkpoint.json     # 增量断点（每版块完成后更新）
└── README.md
```

---

## 模块设计

### 1. `config.py` — 版块定义 + 词表 + 速率

```python
# 发现方式：从 forum.php 首页动态抓取（不硬编码，自动更新）
# 但预置38版块的手工分类映射作为 fallback
BOARD_CATEGORIES = {
    2:   "日常·活动",   6:   "技术·装备",
    18:  "日常·闲聊",   24:  "日常·感悟",
    27:  "日常·众生相", 39:  "日常·求助",
    58:  "日常·会刊",   70:  "技术·路线",
    112: "日常·老会员", 115: "日常·日记",
    25:  "远征·2007",   38:  "远征·2008",
    # ... 全部38版块 + 远征/行疆历年
    15:  "站务",        16:  "站务",
    26:  "站务",
}

# 标签词表（七类）
ROUTES = ["黄巢", "北大赛", "玉符河", "怪坡", "药乡", "斗母泉", "七星台", ...]
PROBLEMS = ["扎胎", "摔车", "中暑", "抽筋", "掉链", "断辐条", ...]
ROLES = ["队长", "协理", "前站", "前骑", "前助", "队医", "技术员", "后骑", "摄影", ...]
EVENT_TYPES = ["通知", "总结", "报名", "探路", "选拔", "训练", "比赛", "讨论", "公告"]

# 速率
MIN_DELAY = 1.0          # 最小请求间隔（秒）
MAX_DELAY = 3.0          # 最大请求间隔
BATCH_SIZE = 20          # 每爬 N 个帖子后冷却
COOLDOWN = 10.0          # 批次间冷却
RETRY_DELAY = 60.0       # HTTP 429/503 退避时间
MAX_RETRIES = 3          # 单个 URL 最大重试次数

# 登录凭据（从环境变量读取，代码不硬编码）
# 运行前设置：
#   export CASDU_USER="你的用户名"
#   export CASDU_PASS="你的密码"
CASDU_USER = os.environ.get("CASDU_USER", "")
CASDU_PASS = os.environ.get("CASDU_PASS", "")
```

### 1.5. 登录支持（可选）

Discuz! X3.2 登录流程：

```
POST member.php?mod=logging&action=login&loginsubmit=yes
  username=CASDU_USER
  password=CASDU_PASS
  cookietime=2592000
→ 获取 cookie: dOIi_2132_auth, dOIi_2132_saltkey
```

- **仅当环境变量 `CASDU_USER` + `CASDU_PASS` 均设置时才登录**
- 登录后 cookie 存入 `requests.Session`，后续请求自动携带
- 实测：**全部 38 个版块均可在匿名状态下通过 archiver 访问**，登录不是爬取的前置条件
- 登录的价值：未来如发现需登录才能查看的版块，或需访问非 archiver 的富文本页面时使用
- 密码不作为命令行参数，不写入日志，不在代码中硬编码

### 2. `utils.py` — HTTP + 解析

关键函数：

- **`create_session()`** → 创建 `requests.Session`，若环境变量有凭据则自动登录
  - 设置 User-Agent: `casdu-archiver/1.0 (bbs.casdu.cn; data preservation)`
  - 登录：POST `member.php?mod=logging&action=login&loginsubmit=yes`

- **`fetch(url, session)`** → 返回 GBK→UTF-8 解码后的 HTML 文本
  - 每次调用后 `sleep(random.uniform(MIN, MAX))`
  - 遇 429/5xx → 退避重试（最多 3 次，间隔 60s）

- **`discover_boards(session)`** → 从 `forum.php` 首页抓取完整版块列表 `[(fid, name), ...]`

- **`parse_board_page(html)`** → 从 archiver 版块页提取 `[(tid, title), ...]`
  - Regex: `<li><a href="?tid-(\d+).html">(.*?)</a>`
  - 提取页导航最大页码

- **`parse_thread_posts(html)`** → 从 archiver 帖子页提取 `[{author, time, content}, ...]`
  - 以 `<p class="author">`（或 `<div class="author">`）为边界切分，通用 pattern: `<\w+\s+class="author">`
  - 每个 block 中提取 `<strong>` (作者), `发表于` (时间), `</p>`（或 `</div>`）后正文
  - 楼层号 = (page-1)*15 + 位置序号

- **`clean_content(raw)`** → 去除 `<br/>`, `<font>`, `<strong>`, `&nbsp;` 等标签，保留纯文本段落
  - **不提取**手机号、邮箱、QQ号等结构化个人信息（正文整体保留但标签引擎不针对隐私字段）

### 3. `classifier.py` — 标签引擎

**输入**：帖子标题 + 正文文本  
**输出**：`{tags, year, season, routes, event_type, roles, problems}`

七类标签的匹配逻辑：

```
1. year + season: 正则 (19|20)\d{2} + \d{2}[春秋暑寒]
2. event_type: 标题含"通知"/"总结"/"报名"/"探路"/"选拔"等
3. routes: 预置路线词典 → 标题+正文前500字匹配
4. roles: 预置角色词典 → 全正文匹配
5. problems: 预置问题词典 → 正文匹配
6. activity_type: 标题含"拉练"/"远征"/"体训"/"比赛"/"行疆"/"小队"
7. tags: 以上所有标签的合并去重
```

### 4. `storage.py` — 持久化

**threads.jsonl** — 每行一个 post，追加写入（仅公开信息，不含隐私字段）：
```json
{"tid":14559,"fid":2,"board":"活动专区","title":"2026春黄巢拉练总结帖",
 "author":"筺橙汁","floor":1,"post_time":"2026-04-16 11:54:21",
 "content":"纯文本正文...","tags":["拉练","黄巢","2026春"],
 "sticky":0,
 ...}
```

**index.db** — SQLite 四张表：
- `threads(tid INT, fid INT, title TEXT, first_author TEXT, post_count INT, sticky INT DEFAULT 0, tags TEXT, ...)`
- `posts(pid INT PK, tid INT, author TEXT, floor INT, post_time TEXT, content TEXT)`
- `fts_posts` — FTS5 全文索引虚拟表
- `sticky` 字段：`0`=普通帖, `1`=版块置顶, `2`=全局置顶

**checkpoint.json** — 断点恢复：
```json
{
  "completed_boards": {"2": true, "6": false, ...},
  "board_page_progress": {"2": 5, "6": 0},
  "thread_progress": {"2": 14559},
  "last_full_crawl": "2026-06-18T23:00:00",
  "highest_tid_seen": 14690
}
```

### 5. `scraper.py` — 主控

```
全量模式 (--full)
  1. discover_boards() → 38个版块
  2. for each board:
     a. 爬 archiver 版块首页，获取总页数
     b. for page in 1..max_page:
        - 爬 ?fid-X.html&page=N
        - 解析线程列表 → 存入 (fid, tid, title, sticky) 四元组
        - 同一 tid 在 ≥3 个版块出现 → 标记 sticky=2（全局置顶），仅首个版块保留
        - 仅在 1 个版块 + 列表前部 → 可选 sticky=1（版块置顶，需主站验证 icn_pt）
     c. 更新 checkpoint（版块完成标记）
  3. for each tid:
     a. 爬 ?tid-X.html，获取总页数
     b. for page in 1..max_page:
        - 爬 ?tid-X.html&page=N
        - 解析 posts → classify → 写 JSONL + DB
     c. 按 batch_size 冷却
     d. 更新 checkpoint（帖子完成标记）

  3.5（可选）互动元数据抓取 (--with-meta)
    对每个帖子，额外请求主站 forum.php?mod=viewthread&tid={tid}&page={N}，
    提取每层楼的互动数据：评分（参与人数/金币/评分详情）、
    支持反对（打气/爆胎）、收藏数、点评（楼中楼短评）。
    元数据按楼层位置匹配（archiver 无 pid），
    写入 JSONL 的 meta 字段和 posts 表的 meta 列（JSON 格式）。
    启用后每条帖子额外约 1 次请求，全量耗时增加（~2.5h）。

增量模式 (--incremental)
  1. 读 checkpoint.json
  2. 对每个版块：只爬首页+第2页，对比 tid
  3. 新 tid → 全量抓取该帖
  4. 旧 tid → 只检查是否有新回复（最后一页对比）
  5. 更新 checkpoint
```

**安全措施**：
- 每次请求前 `sleep(1.0~3.0)` 随机
- 每 20 个帖子后额外 `sleep(10s)`
- 失败 URL 记录到 `failed_urls.log`，不中断
- 每个版块完成后立即写 checkpoint（崩溃可恢复）
- 支持 `--dry-run` 只收集 URL 不下载

---

## 实现顺序

1. `config.py` → 版块分类 + 词表 + 速率常量
2. `utils.py` → HTTP + 编码 + HTML 解析
3. `storage.py` → JSONL + SQLite + checkpoint
4. `classifier.py` → 标签引擎
5. `scraper.py` → 全量 + 增量主流程
6. `README.md`

---

## 验证方法

1. `--dry-run` 干跑 → 打印将访问的 URL 列表和预计耗时
2. `--fid 152` 单版块 → 2026支教队（最新、最小版块）验证全链路 ✅ 已通过
3. 手动对比已知帖子（如 tid=14559）的 JSONL 输出
4. 全量后跑增量 → 确认增量数 = 0
5. 隐私审查：grep JSONL 输出确认不含手机号/邮箱/身份证号等结构化隐私字段
6. 置顶帖检查：确认全局置顶帖（如"论坛使用指南"）未被重复索引

---

## 依赖

- Python 3.8+, `requests`, 标准库（`json, sqlite3, time, random, argparse, re, pathlib, logging, urllib`）
- 不用 beautifulsoup4 — archiver HTML 足够简单，regex 完全胜任

---

## 设计来源

标签引擎的字典匹配 + 正则提取模式参考了 chexie-knowledge 的 `build_entities.py`；`normalize_text()` 清洗逻辑和 `split_text()` 分块逻辑也复用自同一项目。

## 附录：fid=152 首次爬取调试记录

### 概览

首次执行 `python scraper.py --full --fid 152`，预期爬取 2026 支教队版块，实际遇到 3 个 bug，经逐层排查后全部修复，最终 16 秒完成 8 主题 135 帖归档。

### 问题 1：discover_boards() 返回 1 个版块（预期 38 个）

**现象**：日志显示"发现 1 个版块"，后续 `--fid 152` 找不到目标版块直接退出。

**排查**：打印 `forum.php` 首页 HTML，发现实际匹配到了 75 个 `forumdisplay` 链接（去重后 38 个 fid），但 `discover_boards()` 只返回了 1 个。

**根因**：每个版块在首页有**两个链接**——图标链接（内含 `<img>` 标签，无文字）和文字链接。原代码先去重再判断 title 是否为空：
```python
if fid in seen: continue     # ← 图标链接的 fid 先进入 seen
seen.add(fid)
title = re.sub(r'<[^>]+>', '', raw_title)  # 图标链接清洗后为空
if title: boards.append(...)  # ← 空 title 被跳过，但 fid 已占坑
```
文字链接到来时 `fid in seen` 为 True，被当作重复跳过。

**修复**：交换顺序——**先清洗 title、判断非空、再去重**：
```python
title = re.sub(r'<[^>]+>', '', raw_title).strip()
if not title: continue       # ← 图标链接在此过滤
if fid in seen: continue
seen.add(fid)
```

### 问题 2：parse_thread_posts() 返回 0 帖（预期有内容）

**现象**：成功进入 Phase 3 爬取 8 个主题帖，但每个帖子页都显示"0 帖"。

**排查**：直接抓取 tid=14526 的 archiver 页面，搜索 `class="author"` 发现存在 4 个匹配，但正则切分结果为 1。检查 HTML 源码：

```html
<!-- 实际结构 -->
<p class="author">
    <strong>FORCE</strong>
    发表于 2026-4-3 10:52:36
</p>
```

**根因**：Discuz! X3.2 的 archiver 中，作者信息使用的是 **`<p class="author">`** 而非我之前假设的 `<div class="author">`。原正则 `re.split(r'<div\s+class="author">', ...)` 永远匹配不到。

**修复**：将切分正则改为通用标签匹配：
```python
# 修复前
re.split(r'<div\s+class="author">', html)

# 修复后
re.split(r'<\w+\s+class="author">', html)
```
同时修改正文提取逻辑——找到 `</p>` 而非 `</div>` 作为作者块的结束标记。

### 问题 3：日志 UnicodeEncodeError

**现象**：运行过程中出现 `UnicodeEncodeError: 'gbk' codec can't encode character '✓'`（✓ 字符）。

**根因**：Windows Git Bash 的终端编码为 GBK，无法输出 `✓`、`⏭`、`❌` 等 Unicode 符号。Python `logging.StreamHandler` 直接写 `sys.stdout`，触发编码错误。

**修复**：两步处理——
1. 将所有 Unicode 符号替换为 ASCII 标记：`✓→[OK]`、`⏭→[SKIP]`、`⏸→[WAIT]`、`❌→[ERR]`
2. 为 StreamHandler 添加 `safe_write` 包装，捕获 `UnicodeEncodeError` 后降级为 ASCII 输出

### 问题 4：登录 System Error

**现象**：设置 `CASDU_USER`/`CASDU_PASS` 后，登录 POST 返回 `bbs.casdu.cn - System Error`。

**排查**：Discuz! X3.2 的登录表单需要 `formhash`（CSRF token），首次访问 `forum.php` 时服务端会下发。尝试在 POST 中加入 `formhash=80d54670` 后依然报错。推测登录流程还需要 `loginhash` 或 JS 计算的其他字段。

**处理**：由于全部 38 版块均可匿名访问 archiver，**降级为匿名模式**。环境变量未设置时自动跳过登录，设置时若登录失败也以匿名模式继续爬取。登录能力保留在代码中，后续如需访问登录可见版块时可进一步调试。

### 修复后的验证

```bash
$ python scraper.py --full --fid 152
Phase 1: 发现 38 个版块          # 问题1已修复
Phase 2: 收集到 8 个主题帖       # 正常
Phase 3: 爬取帖子内容
  tid=10907: 1/1页 10帖         # 问题2已修复
  tid=10004: 1/1页 11帖
  tid=8654:  3/3页 37帖         # 跨页正常
  ...
  共 135 帖归档                 # 数据验证通过
  耗时: 16s
```

JSONL 输出验证：中文字段完整可读，标签正确识别（tid=14526 → `[2026, 支教]`），FTS5 索引同步（135 entries）。
