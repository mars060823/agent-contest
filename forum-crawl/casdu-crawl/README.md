# bbs.casdu.cn 论坛爬虫

山东大学自行车协会论坛（bbs.casdu.cn）全量数据归档工具。

- **目标站点**：[bbs.casdu.cn](https://bbs.casdu.cn) — Discuz! X3.2，GBK 编码
- **爬取入口**：版块列表用 `forumdisplay.php`（获取精华/置顶/关闭标记），帖详情用 `archiver/`（纯文本，对服务器压力最小）
- **输出格式**：JSONL（行式 JSON）+ SQLite（含 FTS5 全文搜索）
- **自动标签**：年份、学期、事件类型、活动类型、路线、角色、问题 7 类 + 精华等级 + 置顶等级 + 关闭状态
- **可选功能**：主站互动元数据（评分/支持反对/收藏/点评）+ 用户信息采集

---

## 快速开始

### 前提条件

| 条件 | 说明 |
|------|------|
| **Python** | 3.8 或更高版本 |
| **操作系统** | Windows / Linux / macOS 均可 |
| **网络** | 能访问 https://bbs.casdu.cn |
| **磁盘空间** | 全量爬取约需 200 MB（JSONL + SQLite） |
| **登录** | 不需要 — 全部版块匿名可访问 |

### 安装

```bash
# 1. 下载项目（git clone 或直接下载 ZIP 解压）
git clone <repo-url> casdu-crawl
cd casdu-crawl

# 2. 安装依赖（仅需 requests）
pip install -r requirements.txt
```

项目仅依赖 `requests`，无需 beautifulsoup4、lxml 等重型库 — archiver 页是纯 HTML，正则表达式完全胜任。

### 运行

```bash
# 快速验证：Demo 抽样爬取（2~3 分钟，产出约 80 主题 1000 条记录）
python scripts/run_demo.py

# 单版块测试（如 2026 支教队版块，几十帖，十几秒完成）
python scripts/run_scraper.py --full --fid 152

# 全量爬取（38 个版块，约 14,690+ 帖，预计 1.2 小时）
python scripts/run_scraper.py --full

# 全量 + 互动元数据（耗时约 2.5 小时）
python scripts/run_scraper.py --full --with-meta
```

模块方式运行同样有效：

```bash
python -m casdu_crawl.scraper --full
```

### 运行后产物

```
data/
├── demo/
│   ├── threads.jsonl    # Demo 抽样输出
│   └── summary.json     # 抽样报告
├── threads.jsonl        # 正式爬取输出（所有楼层）
├── index.db             # SQLite 索引（含 FTS5）
├── checkpoint.json      # 断点记录（崩溃后可续传）
├── known_uids.txt       # 收集到的所有 UID（--with-meta 模式自动生成）
└── users.jsonl          # 用户公开资料（运行 crawl_users.py 后生成）
```

---

## 命令参考

### 爬虫脚本（`scripts/run_scraper.py`）

| 命令 | 说明 |
|------|------|
| `python scripts/run_scraper.py --dry-run` | 仅收集 URL 列表和估算耗时，不实际下载 |
| `python scripts/run_scraper.py --full` | 全量爬取全部 38 个版块 |
| `python scripts/run_scraper.py --full --fid 152` | 只爬指定版块（用于测试） |
| `python scripts/run_scraper.py --full --with-meta` | 全量爬取 + 主站互动元数据 |
| `python scripts/run_scraper.py --incremental` | 增量更新（仅爬上次之后的新帖和新回复） |
| `python scripts/run_scraper.py --incremental --with-meta` | 增量更新 + 互动元数据 |
| `python scripts/run_scraper.py --info` | 查看当前状态（上次爬取时间、已归档数等） |

### Demo 抽样脚本（`scripts/run_demo.py`）

| 命令 | 说明 |
|------|------|
| `python scripts/run_demo.py` | 默认：每版块随机 3 帖 + 全部置顶帖 |
| `python scripts/run_demo.py --samples 5` | 每版块随机 5 帖 |
| `python scripts/run_demo.py --fid 2` | 只抽指定版块 |
| `python scripts/run_demo.py --seed 123` | 换随机种子（默认 42，可复现） |

> **Demo 抽样策略**：扫描全部 38 个版块前两页，通过跨版块出现频率自动识别全局置顶帖（同一 tid 出现在 ≥3 个版块），置顶帖全部收录；每个版块再从普通帖中随机抽取指定数量。输出 `data/demo/threads.jsonl`（与主爬虫同构）和 `data/demo/summary.json`（抽样报告）。

### 用户信息采集（`scripts/crawl_users.py`）

在 `--with-meta` 模式爬取完成后运行，从已爬取的数据中收集所有已知 UID 并抓取用户公开资料（积分/威望/用户组/注册时间等）。

| 命令 | 说明 |
|------|------|
| `python scripts/crawl_users.py --dry-run` | 干跑：统计可匹配的用户数 |
| `python scripts/crawl_users.py` | 全量爬取所有已知 UID 的资料 |
| `python scripts/crawl_users.py --limit 10` | 测试：只爬前 10 个 |
| `python scripts/crawl_users.py --resume` | 断点续爬 |

**工作流程**：

1. 从 `--with-meta` 爬取时生成的 `data/known_uids.txt` 读取所有已知 UID（帖子作者 + 评分者 + 点评者）
2. 从 `data/threads.jsonl` 的 meta 字段补充 UID→用户名映射
3. 逐个访问 `home.php?mod=space&uid=X&do=profile` 提取公开资料
4. 输出 `data/users.jsonl`

### author_uid 补全（`scripts/enrich_uids.py`）

不加 `--with-meta` 爬取时，JSONL 只有作者名没有 UID。爬取用户资料后可补全：

| 命令 | 说明 |
|------|------|
| `python scripts/enrich_uids.py` | 补全 `data/threads.jsonl` 中缺失的 `author_uid` |
| `python scripts/enrich_uids.py --dry-run` | 仅统计覆盖率，不写入 |
| `python scripts/enrich_uids.py --input other.jsonl` | 指定输入文件 |

**工作流程**：

1. 从 `data/users.jsonl` 读取 `username → uid` 映射
2. 逐行扫描 `data/threads.jsonl`，为缺失 `author_uid` 的记录按作者名匹配
3. 无法匹配的作者设为 `null`
4. 原子写入（临时文件 + rename，中断不丢数据）

> 幂等：已存在 `author_uid` 的记录不会被覆盖。

---

## 登录凭据（可选）

全部 38 个版块均可在匿名状态下访问（forumdisplay.php + archiver），**不需要登录**。

如果将来需要访问登录可见的版块或富文本页面，可设置环境变量：

```bash
# Windows (cmd)
set CASDU_USER=你的用户名
set CASDU_PASS=你的密码

# Windows (PowerShell)
$env:CASDU_USER=”你的用户名”
$env:CASDU_PASS=”你的密码”

# Linux / macOS
export CASDU_USER=”你的用户名”
export CASDU_PASS=”你的密码”
```

**安全说明**：用户名和密码仅从环境变量读取，不硬编码在代码中，不写入日志。当前 Discuz! X3.2 的登录需要 formhash CSRF token，自动登录暂不可用（会降级为匿名模式继续运行）。

---

## 速率控制

为保护服务器，爬虫内置三层限速：

| 措施 | 参数 |
|------|------|
| 请求间隔 | 1.0 ~ 3.0 秒（随机） |
| 批次冷却 | 每 20 帖额外休息 10 秒 |
| 错误退避 | HTTP 429 / 503 后退避 60 秒，最多重试 3 次 |

User-Agent 声明为归档用途：`casdu-archiver/1.0 (bbs.casdu.cn; data preservation project; respects robots.txt; max 1 request per 1-3 seconds)`

全量爬取预计耗时约 **1.2 小时**（不含 `--with-meta`），加 `--with-meta` 约 **2.5 小时**。

---

## 输出格式

### 文件结构

```
data/
├── demo/
│   ├── threads.jsonl    # Demo 抽样（与正式格式一致）
│   └── summary.json     # 抽样报告
├── threads.jsonl        # 所有楼层，每行一条 JSON
├── index.db             # SQLite，含 threads / posts / fts_posts / ratings 四表
├── checkpoint.json      # 断点记录，支持崩溃后续传
├── known_uids.txt       # UID 集合文件（--with-meta 自动生成，每行一个整数）
└── users.jsonl          # 用户资料（运行 crawl_users.py 生成）
```

### threads.jsonl 单条记录（示例数据为虚构）

```json
{
  “tid”: 8932, “fid”: 6,
  “board”: “关于单车”, “category”: “技术·装备”,
  “title”: “【技术部】2025秋自行车检车标准及流程”,
  “author”: “骑车看海”, “floor”: 3,
  “page”: 1, “position”: 3,
  “post_time”: “2025-10-12T15:04:38+08:00”,
  “content”: “三、变速系统检查\n\n1. 前拨定位：将链条挂至大盘大飞…”,
  “content_len”: 87,
  “url”: “https://bbs.casdu.cn/forum.php?mod=viewthread&tid=8932”,
  “digest”: 0, “sticky”: 0, “closed”: 0,
  “reply_to_floor”: null, “reply_to_user”: “”,
  “real_pid”: 164204,
  “tags”: [“技术”, “检车”, “2025秋”, “规程”],
  “year”: “2025”, “season”: “秋”,
  “event_type”: “通知”, “activity_type”: “”,
  “routes”: [], “roles”: [“技术员”, “队长”], “problems”: [],
  “meta”: {
    “author_uid”: 780,
    “rating_count”: 3,
    “rating_coins”: 35,
    “rating_details”: [
      {“uid”: 2148, “username”: “山风”, “coins”: 15, “reason”: “很详细，收藏了”},
      {“uid”: 3507, “username”: “追风少年”, “coins”: 10, “reason”: “感谢分享”},
      {“uid”: 1182, “username”: “骑行侠”, “coins”: 10, “reason”: “赞一个!”}
    ],
    “recommend_add”: 5, “recommend_subtract”: 0,
    “favorite_count”: 12,
    “comment_count”: 2,
    “comments”: [
      {“uid”: 3507, “username”: “追风少年”, “content”: “碟刹间隙那段能再详细点吗”},
      {“uid”: 2148, “username”: “山风”, “content”: “已收藏，寒假回家自己调车用”}
    ]
  }
}
```

### 字段说明

每条记录包含四类字段，来源和触发条件各不相同：

| 字段 | 类型 | 来源 | 说明 |
|------|------|------|------|
| `tid` | int | 系统 | 帖子 ID，Discuz! 自增主键 |
| `fid` | int | 系统 | 版块 ID（38 个版块各有编号） |
| `board` | str | 系统 | 版块名称 |
| `category` | str | 脚本 | 版块分类（如”技术·装备””远征·2025”） |
| `title` | str | 页面 | 帖子标题 |
| `author` | str | 页面 | 论坛用户名（非真实姓名） |
| `author_uid` | int\|null | 脚本 | 作者 UID。`--with-meta` 模式或运行 `enrich_uids.py` 后自动补全 |
| `floor` | int | 页面 | 楼层号 |
| `page` | int | 页面 | 该楼层在 archiver 的第几页 |
| `position` | int | 页面 | 该楼层在当页的第几个 |
| `post_time` | str | 页面 | 发帖时间，ISO-8601 格式 `YYYY-MM-DDTHH:MM:SS+08:00` |
| `content` | str | 页面 | 正文纯文本，已清洗 HTML 标签 |
| `content_len` | int | 脚本 | 正文字符数 |
| `thread_total_floors` | int | 脚本 | 所在主题帖的总楼层数 |
| `url` | str | 脚本 | 主站帖子直链 |
| `digest` | int | 版块列表 | 精华等级：0=普通，1/2/3=精华（从 forumdisplay.php 图标提取） |
| `sticky` | int | 版块列表 | 置顶等级：0=普通，1=版块置顶，2=分区置顶，3=全局置顶（从 `pin_N.gif` 图标提取） |
| `closed` | int | 版块列表 | 关闭状态：0=开放，1=关闭（从 `folder_lock.gif` 图标提取） |
| `reply_to_floor` | int\|null | 页面 | 回复引用的目标楼层号（从 archiver 正文开头"回复 N# xxx"提取） |
| `reply_to_user` | str | 页面 | 回复引用的目标用户名 |
| `tags` | list | 脚本 | 全部标签的合并（以下 6 类的并集） |
| `year` | str | 脚本 | 年份，正则提取自标题（如 `2025`） |
| `season` | str | 脚本 | 学期，春/秋/暑/寒 |
| `event_type` | str | 脚本 | 事件类型：通知/总结/报名/探路/选拔… |
| `activity_type` | str | 脚本 | 活动类型：拉练/远征/体训/比赛… |
| `routes` | list | 脚本 | 路线名，词典匹配（如 `[“怪坡”,”药乡”]`） |
| `roles` | list | 脚本 | 职务，词典匹配（如 `[“队长”,”队医”]`） |
| `problems` | list | 脚本 | 问题类型，词典匹配（如 `[“扎胎”,”摔车”]`） |
| `real_pid` | int | 主站 | **仅在指定 `--with-meta` 时出现**，Discuz 论坛原生帖子 ID |
| `meta` | dict | 主站 | **仅在指定 `--with-meta` 时出现**，见下表 |

### meta 子字段（仅 `--with-meta`）

| 字段 | 类型 | 说明 |
|------|------|------|
| `author_uid` | int\|null | 帖子作者 UID（从主站 viewthread 页面提取） |
| `rating_count` | int | 评分参与人数 |
| `rating_coins` | int | 评分金币总计 |
| `rating_details` | list | 每条评分 `{uid, username, coins, reason}` |
| `recommend_add` | int | 支持数（打气） |
| `recommend_subtract` | int | 反对数（爆胎） |
| `favorite_count` | int | 收藏数 |
| `comment_count` | int | 点评（楼中楼）条数 |
| `comments` | list | 每条点评 `{uid, username, content}` |

`--with-meta` 模式下，爬虫自动将所有遇到的 UID（作者 + 评分者 + 点评者）收集写入 `data/known_uids.txt`（排序去重）。之后运行 `python scripts/crawl_users.py` 即可批量抓取这些用户的公开资料。

**爬取范围总结**：

| 命令 | 爬取范围 |
|------|---------|
| `python scripts/run_scraper.py --full` | 以上**除 `meta` 和 `real_pid` 外**的全部字段 |
| `python scripts/run_scraper.py --full --with-meta` | 以上**全部**字段（含 `meta` 互动数据） |
| `python scripts/run_demo.py` | 同上（抽样子集，格式完全一致） |

不加 `--with-meta` 时：Phase 2 使用 `forumdisplay.php` 收集版块列表（含精华/置顶/关闭标记），Phase 3 使用 `archiver/` 获取帖子正文（轻量、快速）。加了 `--with-meta` 之后每个 archiver 分页（每 15 帖）额外请求一次主站 `forum.php?mod=viewthread` 页面提取评分/支持反对/收藏/点评数据 + 真实 pid，全量耗时约从 1.2 小时增至 2.5 小时。

### SQLite 表结构

| 表 | 用途 | 关键字段 |
|----|------|---------|
| `threads` | 主题帖（每个 tid 一行） | tid, fid, title, post_count, digest, sticky, closed, tags, year, … |
| `posts` | 回帖（每个楼层一行） | pid, real_pid, tid, author, floor, content, meta, reply_to_floor, reply_to_user, … |
| `fts_posts` | FTS5 全文索引 | title, author, content（触发器自动同步） |
| `ratings` | 评分记录（从 meta.rating_details 拆出） | tid, floor, rater_uid, rater_name, coins, reason |

---

## 隐私原则

- 仅抓取论坛**公开页面**（forumdisplay.php + archiver + forum.php），不访问后台或数据库
- 用户信息**仅记录论坛显示名**，不做真实姓名/手机号/邮箱/QQ 等结构化提取
- 标签引擎仅针对路线、事件、角色、问题等**公开议题**，不将隐私信息作为标签
- 保留用户主动公开发布的正文内容，但不做隐私字段挖掘

---

## 项目结构

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
│   ├── crawl_users.py           # 用户信息采集 CLI
│   └── enrich_uids.py           # author_uid 补全工具
├── docs/
│   └── CODE-GENERATE-PLAN.md    # 实现计划 + 调试记录
├── data/                        # 爬取产出（运行后生成）
├── requirements.txt
└── README.md
```

---

## 后续

Phase 2（数据库与检索）和 Phase 3（AI 问答与权重）的设计方案见 [docs/CODE-GENERATE-PLAN.md](docs/CODE-GENERATE-PLAN.md)。

> **注意**: `convert_for_retrieval.py` 和 `run_convert.py` 已移除，Phase 2 将在后续重新设计和实现。
