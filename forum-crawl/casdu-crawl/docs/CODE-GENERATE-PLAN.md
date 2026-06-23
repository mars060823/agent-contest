# bbs.casdu.cn 论坛爬虫 — 实现计划

## 项目概述

为山东大学自行车协会论坛（bbs.casdu.cn）编写全量爬虫，将帖子数据抓取、分类、标签化后本地存储，支持后续增量更新。输出到 `D:\ziYuan\github\casdu`。

**核心约束**：
- 仅抓取公开可见的论坛页面（forumdisplay.php + archiver + forum.php），不访问后台/数据库
- 仅记录用户名等基本公开信息，不提取真实姓名/手机号/邮箱等隐私数据
- 区分置顶帖与普通帖，全局置顶帖不重复索引

**下游场景**：爬取数据的最终用途是作为 AI 问答智能体的知识库（论坛数据 → 检索 → 生成回答）。

本项目分三个阶段推进：
- **Phase 1: 爬虫脚本**（当前重点）— 完整抓取全量帖子数据，在减小服务器压力的前提下保证爬取效率
- **Phase 2: 数据库与检索**（规划中）— 构建支持中文全文搜索的检索引擎
- **Phase 3: AI 问答与权重**（规划中）— 排序加权与查询理解，提升 AI 问答质量

---

# Phase 1: 爬虫脚本

> 目标：完整抓取 bbs.casdu.cn 全量帖子数据，选用 Archiver 入口（纯文本、低负载），单线程 + 随机间隔控制服务器压力，checkpoint 支持断点恢复。

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
| fid=152 实测 | 8 主题 135 帖，19s 完成，精华/置顶/pid 字段验证通过 | 测试通过 |
| forumdisplay 替代 archiver | Phase 2 每页 1 次请求（减半），获取 digest/sticky/closed + author/replies | ✅ 已验证 |

---

## 隐私与合规原则

### 1. 仅抓取公开信息

- 所有数据来源仅为 bbs.casdu.cn **公开可访问**的页面（`forumdisplay.php` + `archiver/` + `forum.php`）
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

## 爬取字段清单

从 Archiver 页面可获取的字段及取舍决策：

| 字段 | 爬取 | 说明 |
|------|:--:|------|
| 帖子标题 | ✅ | 必须 |
| 作者用户名 | ✅ | 公开信息，去重用 |
| 发帖时间 | ✅ | 增量爬取的时间锚点 |
| 帖子正文（纯文本） | ✅ | 核心数据 |
| 楼层号 | ✅ | 从页码 + 位置推算，零额外请求 |
| 版块 ID / 名称 | ✅ | 版块列表页自带 |
| 置顶标记 | ✅ | 从 forumdisplay.php 的 `pin_N.gif` 图标提取（0=普通, 1=版块, 2=分区, 3=全局） |
| 精华标记 | ✅ | 从 forumdisplay.php 的 `digest_N.gif` 图标提取（0/1/2/3） |
| 关闭标记 | ✅ | 从 forumdisplay.php 的 `folder_lock.gif` 检测（0/1） |
| 互动元数据（评分/支持反对/收藏/点评） | ⚠️ 可选 | `--with-meta`，每条额外 1 次主站请求，全量耗时 +~2.5h |
| 真实 pid（Discuz 楼层 ID） | ⚠️ 可选 | `--with-meta` 模式下自动提取并存到 JSONL + SQLite |
| 作者 UID | ⚠️ 可选 | `--with-meta` 自动收集到 `known_uids.txt`，配合 `crawl_users.py` 批量抓取用户资料 |
| 用户资料（积分/威望/用户组/注册时间） | ⚠️ 可选 | 需先 `--with-meta` 爬取，再 `python scripts/crawl_users.py`，零额外请求 |
| 图片 URL | ❌ | Archiver 不提供 |

> **数据来源策略**：Phase 2 使用 `forumdisplay.php` 收集线程列表（含精华/置顶/关闭标记），Phase 3 使用 `archiver/` 获取帖子正文（轻量、低负载）。`--with-meta` 模式下额外请求 `forum.php?mod=viewthread` 获取互动数据。

---

## 置顶、精华、关闭标记

Discuz! 论坛中，管理员可对帖子进行整理操作：置顶（版块/分区/全局）、精华（1/2/3 级）、高亮、关闭、分类等。

### 识别方式（forumdisplay.php）

Phase 2 直接使用 `forumdisplay.php`（非 archiver）收集线程列表，从 HTML 中提取：

- **置顶等级（sticky）**：`<tbody id="stickthread_TID">` + 图标 `pin_1.gif`（版块） / `pin_2.gif`（分区） / `pin_3.gif`（全局）
- **精华等级（digest）**：图标 `digest_1.gif` / `digest_2.gif` / `digest_3.gif`
- **关闭状态（closed）**：图标 `folder_lock.gif` 或 `title="关闭的主题"`

### 实现位置

- `utils.py` → `parse_forumdisplay_page()` 解析 tbody 块，返回 `{tid: {title, digest, sticky, closed, author, replies, post_time}}`
- `scraper.py` Phase 2 → `make_forumdisplay_url()` + `parse_forumdisplay_max_page()` 替代 archiver
- `storage.py` → `threads` 表中 `digest` / `sticky` / `closed` 三列（均为 `INTEGER DEFAULT 0`）

---

## Phase 1 补充项

以下两项为 Phase 1 主体完工后追加的功能增强，均已实现并验证。

### 补充 A：真实 pid 存储 + UID 自动收集

**背景**：Discuz 论坛每层楼有全局唯一的帖子 ID（pid，如 `pid=164204`），嵌在主站 `viewthread` 页面 HTML 中（`id="pidXXXXXX"`）。`parse_thread_meta()` 可从 HTML block ID 提取该值，但此前仅用于楼层位置匹配，匹配后即丢弃，未存入 JSONL/DB。同时，作者 UID 也存在于主站页面的 `home.php?mod=space&uid=XXXXX` 链接中，此前只在 meta 的评分/点评者中附带收集，覆盖率仅 ~33%。

**改动**：

| 文件 | 改动 |
|------|------|
| `utils.py` | `parse_thread_meta()` 返回 dict 中已含 `pid` 和 `author_uid`（先于本次补充项完成） |
| `storage.py` | `posts` 表新增 `real_pid INTEGER` 列 + 迁移 + `insert_post()` 写入 |
| `scraper.py` | `crawl_full()` 中 `record["real_pid"] = matched.get("pid")`；遍历中收集所有 UID（作者+评分+点评）到 `all_uids` set，完成后写入 `data/known_uids.txt` |
| `scraper.py` | `crawl_incremental()` 同步支持（此前增量模式完全不处理 `--with-meta`） |
| `crawl_users.py` | 新增 `collect_known_uids_from_file()` 从 `known_uids.txt` 读取 UID 集合（此前已实现，本次确认可用） |

**效果**：
- JSONL 每条记录在 `--with-meta` 模式下带 `"real_pid": 164204`（Discuz 原生 ID）
- SQLite `posts.real_pid` 列：110/110 帖全填充（fid=152 测试）
- 作者 UID 覆盖率：从 33% → 100%（fid=152: 72/72 作者有 author_uid）
- `known_uids.txt`：85 个 UID（含作者、评分者、点评者），供 `crawl_users.py` 消费

### 补充 B：论坛管理标记（精华/置顶/关闭）

**背景**：管理员可对帖子进行"升降|置顶|高亮|精华|图章|图标|关闭|移动|分类|复制|合并|分割|修复|警告|屏蔽|标签"等整理操作。其中精华（digest）、置顶（sticky）、关闭（closed）三类标记在 `forumdisplay.php` 版块列表页中有对应的图标（`digest_N.gif` / `pin_N.gif` / `folder_lock.gif`），可零额外请求提取。

**改动**：

| 文件 | 改动 |
|------|------|
| `utils.py` | 新增 `parse_forumdisplay_page()` — 解析 `<tbody id="stickthread_|normalthread_">` 块，提取 `{tid: {title, digest, sticky, closed, author, replies, post_time}}` |
| `utils.py` | 新增 `parse_forumdisplay_max_page()` — 从分页链接提取最大页码 |
| `utils.py` | 新增 `make_forumdisplay_url()` — 生成 forumdisplay URL |
| `scraper.py` | Phase 2 从 archiver 改为 forumdisplay.php（每页 1 次请求替代原 2 次，且数据更丰富） |
| `scraper.py` | Phase 3 解包新增 `digest, sticky, closed`，写入 JSONL record + `upsert_thread()` |
| `scraper.py` | `crawl_incremental()` 同步更新 |
| `storage.py` | `threads` 表新增 `digest`, `sticky`, `closed` 列 + 迁移 + `upsert_thread()` 更新 |

**效果**：
- Phase 2 请求数减半（不再同时请求 archiver + forumdisplay），且修复了 archiver 对个别帖子 404 导致数据丢失的 bug
- `sticky`：0=普通, 1=版块置顶, 2=分区置顶, 3=全局置顶（从 `pin_N.gif` 精确区分）
- `digest`：0=普通, 1/2/3=精华等级（从 `digest_N.gif` 提取）
- `closed`：0/1（从 `folder_lock.gif` 检测）
- fid=2（活动专区）实测：12 置顶帖（均为全局 sticky=3）+ 9 精华帖（digest=1/3）

---

## 文件结构

```
casdu-crawl/
├── casdu_crawl/
│   ├── __init__.py              # 包入口
│   ├── config.py                # 配置：38 版块字典 + 7 类标签词表 + 速率常量
│   ├── scraper.py               # 主控：全量 + 增量爬取流程
│   ├── utils.py                 # HTTP、GBK 解码、forumdisplay/archiver 解析、元数据提取
│   ├── classifier.py            # 自动分类标签引擎
│   └── storage.py               # JSONL 写入 + SQLite（FTS5）+ Checkpoint
├── scripts/
│   ├── run_scraper.py           # 爬虫 CLI 入口
│   ├── run_demo.py              # Demo 抽样 CLI 入口
│   └── crawl_users.py           # 用户信息采集 CLI
├── docs/
│   └── CODE-GENERATE-PLAN.md    # 本文件
├── data/                        # 爬取产出（运行后生成）
│   ├── threads.jsonl            # 所有楼层，每行一条 JSON
│   ├── index.db                 # SQLite（threads + posts + fts_posts）
│   ├── checkpoint.json          # 断点记录
│   ├── known_uids.txt           # UID 集合（--with-meta 自动生成）
│   └── users.jsonl              # 用户资料（运行 crawl_users.py 生成）
├── requirements.txt
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

- **`parse_board_page(html)`** → 从 archiver 版块页提取 `[(tid, title), ...]`（demo 脚本仍在使用）

- **`parse_forumdisplay_page(html)`** → 从 forumdisplay.php 版块列表页提取 `{tid: {title, digest, sticky, closed, author, replies, post_time}}`
  - 解析 `<tbody id="stickthread_|normalthread_">` 块
  - 从 `pin_N.gif` 提取置顶等级（1/2/3），从 `digest_N.gif` 提取精华等级
  - **替代 archiver 版块页**作为 Phase 2 的数据源（一次请求获取更丰富的元数据）

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
 "digest":0,"sticky":0,"closed":0,"real_pid":133322,
 ...}
```

**index.db** — SQLite 四张表：
- `threads(tid INT, fid INT, title TEXT, first_author TEXT, post_count INT, digest INT DEFAULT 0, sticky INT DEFAULT 0, closed INT DEFAULT 0, tags TEXT, ...)`
- `posts(pid INT PK AUTOINCREMENT, real_pid INT, tid INT, author TEXT, floor INT, post_time TEXT, content TEXT)`
- `fts_posts` — FTS5 全文索引虚拟表
- `digest`：精华等级 `0`/`1`/`2`/`3`；`sticky`：`0`=普通, `1`=版块, `2`=分区, `3`=全局；`closed`：`0`/`1`

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
     a. 爬 forumdisplay.php 版块首页，获取总页数 + 线程列表
     b. for page in 1..max_page:
        - 爬 forum.php?mod=forumdisplay&fid=X&page=N
        - 解析线程列表 + 精华/置顶/关闭标记 → 存入 (tid, fid, title, board, digest, sticky, closed) 元组
     c. 更新 checkpoint（版块完成标记）
  3. for each tid:
     a. 爬 ?tid-X.html (archiver)，获取总页数
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
6. 置顶帖检查：确认全局置顶帖 sticky=3，版块置顶 sticky=1（从 forumdisplay.php `pin_N.gif` 精确检测）
7. 精华帖检查：`SELECT tid, digest FROM threads WHERE digest>0` — fid=2 检出 9 个精华帖 ✅ 已通过
8. 真实 pid 检查：`SELECT COUNT(*) FROM posts WHERE real_pid IS NOT NULL` — fid=152: 110/110 ✅ 已通过
9. UID 收集检查：`wc -l data/known_uids.txt` — fid=152: 85 个 UID ✅ 已通过

---

## 依赖

- Python 3.8+, `requests`, 标准库（`json, sqlite3, time, random, argparse, re, pathlib, logging, urllib`）
- 不用 beautifulsoup4 — archiver 和 forumdisplay HTML 足够简单，regex 完全胜任

---

## 设计来源

标签引擎的字典匹配 + 正则提取模式参考了 chexie-knowledge 的 `build_entities.py`；`normalize_text()` 清洗逻辑和 `split_text()` 分块逻辑也复用自同一项目。

---

## 范围边界（本阶段明确不做）

| 不做 | 原因 |
|------|------|
| 多线程并发爬取 | 1.2h 全量可接受，无需增加复杂度 |
| 代理池 / 速度档位 | 无反爬压力 |
| 图片 URL 抓取 | Archiver 不提供 |

---

## Phase 1 补充项

以下为 Phase 1 爬虫的后续优化方向，按优先级排列。

### ✅ 已完成

| 项 | 说明 | 涉及文件 |
|----|------|---------|
| 作者 UID 采集 | `--with-meta` 模式从 viewthread 页面自动提取作者 UID，与评分者/点评者 UID 合并写入 `data/known_uids.txt`（排序去重） | `utils.py:459` `scraper.py:138,305-326,382-387` |
| 用户信息爬取 | 独立脚本 `scripts/crawl_users.py`，读取 `known_uids.txt`，批量抓取 `home.php?mod=space&uid=X&do=profile` 公开资料（积分/威望/用户组/注册时间/在线时间等 17 个字段），输出 `data/users.jsonl` | `scripts/crawl_users.py` |

### 🔲 待实施

| 项 | 说明 | 数据来源 | 额外请求 |
|----|------|---------|:--:|
| 真实 pid | 当前使用自增 ID 替代真实 pid。viewthread 页面已包含 `id="pidXXXXX"`，`parse_thread_meta()` 已提取但未存入 post 表 | viewthread (`--with-meta`) | 无 |
| 精华标记 | 版块列表页（forumdisplay.php）有精华图标 CSS 类。在 Phase 2 收集线程列表时同步解析 | forumdisplay.php | 无 |
| 回复数 / 查看数 | 同一页可见：`查看: 402 | 回复: 3`。可直接补入 threads 表 | forumdisplay.php | 无 |
| 编辑记录 | 帖子页底部 "本帖最后由 XXX 于 YYYY-MM-DD HH:MM 编辑"，与 `--with-meta` 同源 | viewthread (`--with-meta`) | 无 |
| 版块描述 / 版主 | 版块列表页顶部有版块简介和版主名，可在 Phase 2 收集线程时同步抓取 | forumdisplay.php | 无 |
| 用户 UID 补全 | 存量数据中 599 位作者（66.5%）无 UID。下次 `--full --with-meta` 可自然补充；存量数据需用独立补采脚本 | viewthread | 805 次 (~30min) |

> 以上待实施项均**零额外请求**（数据源已在现有爬取流程中被请求），只需追加解析逻辑。

---

# Phase 2: 数据库与检索

> 目标：在 Phase 1 获取的数据基础上，构建支持中文全文搜索的检索引擎。检索精度直接影响下游 AI 问答质量。
>
> 状态：**规划中**，基线测量已完成（`baseline_runner.py`），具体检索方案待 Phase 1 完成后启动。

---

## 检索方案选型

**现状问题**：SQLite FTS5 的 `unicode61` 分词器对中文不可用——基线测量（512 条查询）表明 recall@20 = 0.78%。连续 CJK 字符被当作单一 token，只有中文词偶然被 ASCII 分隔时才能命中。当前系统如果已接入 AI 问答，用户绝大多数查询返回空结果。

**方案对比**（~1 万帖、纯 Python 栈）：

| 方案 | 分词 | 召回质量 | 部署成本 |
|------|:---:|:---:|------|
| **Whoosh + jieba**（推荐） | jieba | ⭐⭐⭐⭐ 86.91% | 纯 Python，`pip install whoosh`，增量索引 |
| SQLite FTS5 + jieba 预处理 | 写入侧分词 | ⭐⭐⭐⭐ | 保持 SQLite 架构，但真正注册分词器需 C 扩展 |
| DuckDB FTS | 内置 CJK | ⭐⭐⭐½ | `pip install duckdb`，单文件，备选 |
| `rank_bm25` + jieba（裸算） | jieba | ⭐⭐⭐⭐ 86.91% | 无增量索引，每次全量重算，仅适合基线测量 |

Meilisearch / ES+IK / Tantivy 均需外部服务或编译依赖，与本项目规模不匹配。

**结论**：Whoosh + jieba 是当前最优解——`baseline_runner.py` 已用 `rank_bm25` + jieba 验证过同类方案（recall@20 = 86.91%），Whoosh 是其工程化版本（增量索引、磁盘持久化、BM25 评分 + 查询语法），无需推翻现有架构。

---

## 优化路线图

基线测量后的数据驱动优先级：

| 优先级 | 事项 | 触发数据 |
|--------|------|---------|
| **P0** | Whoosh + jieba 替换 FTS5 | FTS5 recall@20 = 0.78%，基本不可用 |
| **P0** | 过滤空内容帖 | 占 BM25 剩余失败 75% |
| P1 | 相邻楼层上下文合并 | 短回复缺乏区分度 |
| P1 | 查询改写词典（同义词 + 上位词） | 17.9% 失败为查询失配 |
| P2 | 楼层位置权重（首帖加权） | 首帖信息密度最高 |

向量检索暂不引入——BM25 在当前数据量上 recall@20 已达 86.91%，embedding 模型的增量与成本不匹配。

---

## 范围边界（本阶段明确不做）

| 不做 | 原因 |
|------|------|
| Flask Web 离线浏览 | AI 问答不需要浏览器 |
| 词云 / 热力图 / 情感分析等分析模块 | AI 实时推理即可，无需预计算 |
| 帖子引用链 pid 解析 | Archiver HTML 的 blockquote 不含 pid，技术上不可行 |
| 向量检索（embedding） | BM25 召回已足够，增量与成本不匹配 |

---

# Phase 3: AI 问答与权重

> 目标：在检索基础上引入排序加权和查询理解，提升 AI 问答质量。
>
> 状态：**规划中**，具体方案待 Phase 2 检索管线稳定后设计。

---

## 版块权威度

用于检索排序加权（数据驱动，待 Phase 2 验证）：
- 技术类（关于单车、路线攻略）和远征/支教/实践版块 > 活动专区/求助 > 站务 > 闲聊/感悟

## 话题分类体系

按车协实际活动体系组织：
- 暑期活动（远征/行疆/小队）、冬游、日常拉练、修车技巧、装备推荐、安全事故、纳新流程、内部管理

## 查询改写与扩展

- 同义词映射（50 条领域同义词，已储备于 `scripts/synonym_map.py`）
- 上位词映射（80+ 条领域上位词，已储备于 `scripts/hypernym_map.py`）

## 内容质量信号

- 文本长度、结构化程度（替代不可获取的 view_count）

## 安全事故类查询

"答错后果"由 Agent 层面的自检和权威加权保证，数据库只管准确检索。

---

## 范围边界（本阶段明确不做）

| 不做 | 原因 |
|------|------|
| 作者权威评分系统 | 需要引用图 + 领域分类 + 评分模型，量级过大（从 UID 到权威评分中间是一篇硕士论文） |
| 用户 UID / 积分 / 用户组采集 | Phase 1 不爬；Phase 2 按需补充仅用于去重过滤，不追求权威评分 |

---

# 附录

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
