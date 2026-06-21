#!/usr/bin/env python3
"""
bbs.casdu.cn 爬虫 — 数据持久化
JSONL 追加写入、SQLite 索引（含 FTS5）、checkpoint 断点管理
"""

import json
import sqlite3
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger("casdu")


# ============================================================================
# 工具
# ============================================================================

def _sanitize_string(s: str) -> str:
    """清理字符串中的 lone surrogate 字符（GBK 解码残留）。"""
    if not isinstance(s, str):
        return s
    # 尝试编码为 UTF-8，替换无法编码的 surrogate
    return s.encode("utf-8", errors="replace").decode("utf-8")


def _sanitize_record(obj):
    """递归清理 record 中的所有字符串字段。"""
    if isinstance(obj, str):
        return _sanitize_string(obj)
    if isinstance(obj, dict):
        return {k: _sanitize_record(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_record(v) for v in obj]
    return obj


def _safe_dumps(obj) -> str:
    """json.dumps 的安全封装，自动清理 lone surrogate。"""
    return json.dumps(_sanitize_record(obj), ensure_ascii=False)


# ============================================================================
# Threads JSONL
# ============================================================================

class JsonlWriter:
    """逐行追加写入 JSONL 文件。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = None

    def open(self):
        """以追加模式打开文件。"""
        self._fd = self.path.open("a", encoding="utf-8")

    def write(self, record: dict):
        """写入一行 JSON（追加）。

        自动处理 GBK 解码残留的 lone surrogate 字符。
        """
        if self._fd is None:
            self.open()
        line = _safe_dumps(record)
        self._fd.write(line + "\n")
        self._fd.flush()

    def close(self):
        if self._fd:
            self._fd.close()
            self._fd = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()


# ============================================================================
# SQLite 索引
# ============================================================================

SCHEMA_SQL = """
-- 主题帖表（每个 tid 一行）
CREATE TABLE IF NOT EXISTS threads (
    tid         INTEGER PRIMARY KEY,
    fid         INTEGER NOT NULL,
    board       TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    first_author TEXT NOT NULL DEFAULT '',
    first_time  TEXT NOT NULL DEFAULT '',
    post_count  INTEGER NOT NULL DEFAULT 0,
    tags        TEXT NOT NULL DEFAULT '[]',
    year        TEXT NOT NULL DEFAULT '',
    season      TEXT NOT NULL DEFAULT '',
    event_type  TEXT NOT NULL DEFAULT '',
    routes      TEXT NOT NULL DEFAULT '[]',
    roles       TEXT NOT NULL DEFAULT '[]',
    problems    TEXT NOT NULL DEFAULT '[]',
    last_crawl  TEXT NOT NULL DEFAULT '',
    url         TEXT NOT NULL DEFAULT ''
);

-- 回帖表（每层楼一行）
CREATE TABLE IF NOT EXISTS posts (
    pid         INTEGER PRIMARY KEY AUTOINCREMENT,
    tid         INTEGER NOT NULL,
    fid         INTEGER NOT NULL,
    board       TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    author      TEXT NOT NULL DEFAULT '',
    floor       INTEGER NOT NULL DEFAULT 0,
    page        INTEGER NOT NULL DEFAULT 1,
    position    INTEGER NOT NULL DEFAULT 0,
    post_time   TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL DEFAULT '',
    content_len INTEGER NOT NULL DEFAULT 0,
    tags        TEXT NOT NULL DEFAULT '[]',
    category    TEXT NOT NULL DEFAULT '',
    meta        TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (tid) REFERENCES threads(tid)
);

CREATE INDEX IF NOT EXISTS idx_posts_tid ON posts(tid);
CREATE INDEX IF NOT EXISTS idx_posts_fid ON posts(fid);
CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author);
CREATE INDEX IF NOT EXISTS idx_threads_fid ON threads(fid);
CREATE INDEX IF NOT EXISTS idx_threads_category ON threads(category);
CREATE INDEX IF NOT EXISTS idx_threads_year ON threads(year);

-- FTS5 全文搜索虚拟表（内容索引）
CREATE VIRTUAL TABLE IF NOT EXISTS fts_posts USING fts5(
    title,
    author,
    content,
    content='posts',
    content_rowid='pid',
    tokenize='unicode61'
);

-- FTS5 同步触发器
CREATE TRIGGER IF NOT EXISTS posts_ai AFTER INSERT ON posts BEGIN
    INSERT INTO fts_posts(rowid, title, author, content)
    VALUES (new.pid, new.title, new.author, new.content);
END;

CREATE TRIGGER IF NOT EXISTS posts_ad AFTER DELETE ON posts BEGIN
    INSERT INTO fts_posts(fts_posts, rowid, title, author, content)
    VALUES ('delete', old.pid, old.title, old.author, old.content);
END;

CREATE TRIGGER IF NOT EXISTS posts_au AFTER UPDATE ON posts BEGIN
    INSERT INTO fts_posts(fts_posts, rowid, title, author, content)
    VALUES ('delete', old.pid, old.title, old.author, old.content);
    INSERT INTO fts_posts(rowid, title, author, content)
    VALUES (new.pid, new.title, new.author, new.content);
END;
"""


class IndexDB:
    """SQLite 索引数据库管理。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.conn: Optional[sqlite3.Connection] = None

    def open(self):
        """打开数据库并创建表结构。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def insert_post(self, post: dict):
        """插入一条回帖记录（同时触发 FTS5 同步）。"""
        if self.conn is None:
            self.open()
        self.conn.execute(
            "INSERT INTO posts (tid, fid, board, title, author, floor, "
            "page, position, post_time, content, content_len, tags, category, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                post.get("tid", 0),
                post.get("fid", 0),
                post.get("board", ""),
                post.get("title", ""),
                post.get("author", ""),
                post.get("floor", 0),
                post.get("page", 1),
                post.get("position", 0),
                post.get("post_time", ""),
                post.get("content", ""),
                len(post.get("content", "")),
                _safe_dumps(post.get("tags", [])),
                post.get("category", ""),
                _safe_dumps(post.get("meta", {})),
            ),
        )

    def upsert_thread(self, thread: dict):
        """插入或更新主题帖记录。"""
        if self.conn is None:
            self.open()
        self.conn.execute(
            "INSERT OR REPLACE INTO threads "
            "(tid, fid, board, category, title, first_author, first_time, "
            "post_count, tags, year, season, event_type, routes, roles, "
            "problems, last_crawl, url) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                thread.get("tid", 0),
                thread.get("fid", 0),
                thread.get("board", ""),
                thread.get("category", ""),
                thread.get("title", ""),
                thread.get("first_author", ""),
                thread.get("first_time", ""),
                thread.get("post_count", 0),
                _safe_dumps(thread.get("tags", [])),
                thread.get("year", ""),
                thread.get("season", ""),
                thread.get("event_type", ""),
                _safe_dumps(thread.get("routes", [])),
                _safe_dumps(thread.get("roles", [])),
                _safe_dumps(thread.get("problems", [])),
                thread.get("last_crawl", ""),
                thread.get("url", ""),
            ),
        )

    def thread_exists(self, tid: int) -> bool:
        """检查主题帖是否已存在。"""
        if self.conn is None:
            self.open()
        row = self.conn.execute(
            "SELECT 1 FROM threads WHERE tid=? LIMIT 1", (tid,)
        ).fetchone()
        return row is not None

    def post_count_for_thread(self, tid: int) -> int:
        """获取某主题帖已有的回帖数。"""
        if self.conn is None:
            self.open()
        row = self.conn.execute(
            "SELECT COUNT(*) FROM posts WHERE tid=?", (tid,)
        ).fetchone()
        return row[0] if row else 0

    def get_highest_tid(self) -> int:
        """获取已存储的最大 tid。"""
        if self.conn is None:
            self.open()
        row = self.conn.execute(
            "SELECT MAX(tid) FROM threads"
        ).fetchone()
        return row[0] or 0

    def commit(self):
        if self.conn:
            self.conn.commit()

    def close(self):
        if self.conn:
            self.conn.commit()
            self.conn.close()
            self.conn = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *args):
        self.close()


# ============================================================================
# Checkpoint 断点管理
# ============================================================================

DEFAULT_CHECKPOINT = {
    "completed_boards": {},       # {fid: true/false}
    "board_page_progress": {},    # {fid: last_page_crawled}
    "thread_progress": {},        # {fid: last_tid_processed}
    "last_full_crawl": "",        # ISO 时间戳
    "highest_tid_seen": 0,        # 全局最大 tid
    "total_posts_archived": 0,    # 总帖数
}


class Checkpoint:
    """检查点（断点恢复）管理器。

    每个版块完成后立即写入，支持崩溃恢复。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = dict(DEFAULT_CHECKPOINT)  # 深拷贝

    def load(self) -> bool:
        """从文件加载检查点。返回 False 表示不存在。"""
        if not self.path.exists():
            return False
        try:
            with self.path.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            # 合并到默认值（处理新增字段）
            self.data = {**DEFAULT_CHECKPOINT, **loaded}
            logger.info("加载检查点: %s 版块已完成, 最高 tid=%d",
                        sum(1 for v in self.data["completed_boards"].values() if v),
                        self.data["highest_tid_seen"])
            return True
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("检查点损坏: %s，将从头开始", e)
            return False

    def save(self):
        """写入检查点到文件。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def mark_board_complete(self, fid: int):
        """标记某版块的线程列表已全部收集。"""
        self.data["completed_boards"][str(fid)] = True
        self.save()

    def is_board_complete(self, fid: int) -> bool:
        """判断某版块是否已完成线程列表收集。"""
        return self.data["completed_boards"].get(str(fid), False)

    def update_board_page(self, fid: int, page: int):
        """更新版块翻页进度。"""
        self.data["board_page_progress"][str(fid)] = page

    def get_board_page(self, fid: int) -> int:
        """获取版块翻页进度。"""
        return self.data["board_page_progress"].get(str(fid), 0)

    def update_thread_progress(self, fid: int, tid: int):
        """更新帖子的处理进度。"""
        self.data["thread_progress"][str(fid)] = tid

    def get_thread_progress(self, fid: int) -> int:
        """获取帖子的处理进度。"""
        return self.data["thread_progress"].get(str(fid), 0)

    def update_highest_tid(self, tid: int):
        """更新全局最大 tid。"""
        if tid > self.data["highest_tid_seen"]:
            self.data["highest_tid_seen"] = tid

    def increment_posts(self, count: int):
        """增加已归档帖数。"""
        self.data["total_posts_archived"] += count
