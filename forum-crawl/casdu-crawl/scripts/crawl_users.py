#!/usr/bin/env python3
"""
用户信息爬取脚本

从已有爬取数据中收集所有作者，查找其 UID，然后逐一抓取个人资料页，
提取公开的论坛统计信息（积分/威望/用户组/注册时间等）。

用法：
    python scripts/crawl_users.py                     # 全量爬取所有用户
    python scripts/crawl_users.py --dry-run           # 干跑：仅列出待爬用户
    python scripts/crawl_users.py --limit 5           # 测试：只爬前 5 个
    python scripts/crawl_users.py --resume            # 断点续爬

数据来源：
    1. data/threads.jsonl  → 所有作者名 + meta 中已有的 UID
    2. home.php?mod=space&uid=X&do=profile → 用户资料页

输出：
    data/users.jsonl       → 每行一个用户
"""

import argparse
import io
import json
import logging
import re
import sys
import time
import random
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

# 路径设置
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_DIR))

from casdu_crawl.config import (
    BASE_URL, MIN_DELAY, MAX_DELAY,
    TIMEOUT, USER_AGENT,
)
from casdu_crawl.utils import create_session, fetch, format_duration
from casdu_crawl.storage import JsonlWriter

DATA_DIR = PROJECT_DIR / "data"
JSONL_PATH = DATA_DIR / "threads.jsonl"
USERS_PATH = DATA_DIR / "users.jsonl"
CHECKPOINT_PATH = DATA_DIR / "users_checkpoint.json"

logger = logging.getLogger("casdu.users")


# ============================================================================
# UID 收集
# ============================================================================

def collect_known_uid_map() -> dict[int, str]:
    """从已有 JSONL 的 meta 字段中收集已知的 (uid → username) 映射。

    meta.rating_details 和 meta.comments 中包含评分/点评者的 UID 和用户名。
    """
    uid_map: dict[int, str] = {}
    if not JSONL_PATH.exists():
        logger.warning("threads.jsonl 不存在，无法收集已知 UID")
        return uid_map

    logger.info("从 threads.jsonl 收集已知 UID ...")
    with JSONL_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            meta = record.get("meta", {})
            if not isinstance(meta, dict):
                continue

            for rater in meta.get("rating_details", []):
                uid = rater.get("uid")
                name = rater.get("username", "").strip()
                if uid and name:
                    uid_map[uid] = name

            for comment in meta.get("comments", []):
                uid = comment.get("uid")
                name = comment.get("username", "").strip()
                if uid and name:
                    uid_map[uid] = name

    logger.info("  已知 UID: %d 个（来自 meta）", len(uid_map))
    return uid_map


KNOWN_UIDS_PATH = DATA_DIR / "known_uids.txt"


def collect_known_uids_from_file() -> set[int]:
    """从 scraper 输出的 known_uids.txt 读取已收集的 UID 集合。

    此文件由 scraper 的 --with-meta 模式在爬取过程中生成，
    包含帖子作者、评分者、点评者的 UID。
    """
    uids: set[int] = set()
    if not KNOWN_UIDS_PATH.exists():
        return uids
    with KNOWN_UIDS_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.isdigit():
                uids.add(int(line))
    logger.info("  已知 UID: %d 个（来自 known_uids.txt）", len(uids))
    return uids


def collect_unique_authors() -> list[str]:
    """从 JSONL 收集所有不重复的作者名。"""
    authors: set[str] = set()
    if not JSONL_PATH.exists():
        return []

    with JSONL_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            author = record.get("author", "").strip()
            if author:
                authors.add(author)

    logger.info("  唯一作者: %d 个", len(authors))
    return sorted(authors)


# ============================================================================
# 用户名 → UID 查找
# ============================================================================

def lookup_uid_by_username(session, username: str) -> int | None:
    """通过用户名查找 UID。

    使用 Discuz 的 home.php?mod=space&username=URL_ENCODED_NAME 接口。
    如果用户存在，页面 URL 会重定向到 uid=X 或页面内容包含 UID。
    """
    try:
        encoded = urllib.parse.quote(username, encoding="gbk")
        url = f"{BASE_URL}/home.php?mod=space&username={encoded}"
        resp = session.get(url, timeout=TIMEOUT, allow_redirects=True)

        if resp.status_code != 200:
            return None

        # 方法 1：从最终 URL 提取 UID（重定向后 URL 可能包含 uid=）
        final_url = resp.url
        uid_m = re.search(r"[?&]uid=(\d+)", final_url)
        if uid_m:
            return int(uid_m.group(1))

        # 方法 2：从页面内容提取
        html = resp.content.decode("gbk", errors="replace")
        uid_m = re.search(r"UID:\s*(\d+)", html[:5000])
        if uid_m:
            return int(uid_m.group(1))

        # 方法 3：从 avatar.php?uid=X 提取
        avatar_m = re.search(r"avatar\.php\?uid=(\d+)", html[:5000])
        if avatar_m:
            return int(avatar_m.group(1))

        return None
    except Exception as e:
        logger.warning("用户名查找失败 [%s]: %s", username, e)
        return None


# ============================================================================
# 用户资料解析
# ============================================================================

def parse_user_profile(html: str) -> dict | None:
    """从 home.php?mod=space&uid=X&do=profile 页面提取用户信息。

    提取字段（均为公开信息）：
        uid, username,
        user_group, extend_group,
        points, prestige, gold,
        online_hours, reg_time, last_visit, last_activity, last_post,
        friends_count, topics_count, replies_count,
        gender,
    """
    info: dict = {}

    # --- UID + 用户名 ---
    uid_m = re.search(r"UID:\s*(\d+)", html)
    if uid_m:
        info["uid"] = int(uid_m.group(1))
    else:
        return None  # 页面无效

    # 用户名：h2 标签内
    name_m = re.search(r"<h2\s+class=\"mbn\">\s*([^<]+)", html)
    if name_m:
        info["username"] = name_m.group(1).strip()

    # --- 用户组 ---
    ug_m = re.search(
        r"<em\s+class=\"xg1\">用户组[^<]*</em>\s*<span[^>]*><a[^>]*>([^<]+)</a>",
        html,
    )
    info["user_group"] = ug_m.group(1).strip() if ug_m else ""

    # 扩展用户组
    exg_m = re.search(
        r"<em\s+class=\"xg1\">扩展用户组[^<]*</em>\s*([^<]+)</li>",
        html,
    )
    info["extend_group"] = exg_m.group(1).strip() if exg_m else ""

    # --- 统计信息 ---
    stats_map = {
        "points": ("积分", r"<em>积分</em>\s*(\d[\d,]*)"),
        "prestige": ("威望", r"<em>威望</em>\s*(\d[\d,]*)"),
        "gold": ("金币", r"<em>金币</em>\s*(\d[\d,]*)"),
    }
    for key, (label, pat) in stats_map.items():
        m = re.search(pat, html)
        info[key] = int(m.group(1).replace(",", "")) if m else 0

    # --- 活跃概况 ---
    activity_map = {
        "online_hours": ("在线时间", r"<em>在线时间</em>\s*([^<]+)"),
        "reg_time": ("注册时间", r"<em>注册时间</em>\s*([^<]+)"),
        "last_visit": ("最后访问", r"<em>最后访问</em>\s*([^<]+)"),
        "last_activity": ("上次活动时间", r"<em>上次活动时间</em>\s*([^<]+)"),
        "last_post": ("上次发表时间", r"<em>上次发表时间</em>\s*([^<]+)"),
    }
    for key, (label, pat) in activity_map.items():
        m = re.search(pat, html)
        val = m.group(1).strip() if m else ""
        # 清理实体引用
        val = val.replace("&nbsp;", " ").strip()
        info[key] = val

    # --- 统计信息（好友/主题/回帖数） ---
    friends_m = re.search(r"好友数\s*(\d+)", html)
    info["friends_count"] = int(friends_m.group(1)) if friends_m else 0

    topics_m = re.search(r"主题数\s*(\d+)", html)
    info["topics_count"] = int(topics_m.group(1)) if topics_m else 0

    replies_m = re.search(r"回帖数\s*(\d+)", html)
    info["replies_count"] = int(replies_m.group(1)) if replies_m else 0

    # --- 性别 ---
    gender_m = re.search(r"<em>性别</em>\s*([^<\s]+)", html)
    if gender_m:
        g = gender_m.group(1).strip()
        info["gender"] = g if g in ("男", "女") else ""
    else:
        info["gender"] = ""

    # --- 时间戳 ---
    info["crawled_at"] = datetime.now(timezone.utc).isoformat()

    return info


def fetch_user_profile(session, uid: int) -> dict | None:
    """获取并解析指定 UID 的用户资料。"""
    url = f"{BASE_URL}/home.php?mod=space&uid={uid}&do=profile"
    try:
        html = fetch(url, session, force_no_delay=True)
    except Exception as e:
        logger.error("获取用户资料失败 uid=%d: %s", uid, e)
        return None

    if not html:
        return None

    info = parse_user_profile(html)
    if info is None:
        logger.warning("解析用户资料失败 uid=%d", uid)
        return None
    return info


# ============================================================================
# 输出
# ============================================================================

class UsersWriter:
    """追加写入 users.jsonl，支持断点恢复。

    内部使用 JsonlWriter 处理文件 I/O，本类仅增加基于 UID 的去重和进度跟踪。
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._jsonl = JsonlWriter(path)
        self._written_uids: set[int] = set()

    def _load_existing(self):
        """读取已写入的用户 UID（用于断点恢复）。"""
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                    if "uid" in rec:
                        self._written_uids.add(rec["uid"])
                except json.JSONDecodeError:
                    continue

    def open(self):
        self._load_existing()
        self._jsonl.open()

    def is_done(self, uid: int) -> bool:
        return uid in self._written_uids

    def write(self, record: dict):
        self._jsonl.write(record)
        self._written_uids.add(record.get("uid", 0))

    @property
    def count(self) -> int:
        return len(self._written_uids)

    def close(self):
        self._jsonl.close()


# ============================================================================
# 主流程
# ============================================================================

def main():
    # 强制 UTF-8 输出
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

    parser = argparse.ArgumentParser(description="用户信息爬取")
    parser.add_argument("--dry-run", action="store_true", help="干跑：仅列出待爬用户")
    parser.add_argument("--limit", type=int, default=0, help="限制爬取数量（测试用）")
    parser.add_argument("--resume", action="store_true", help="断点续爬")

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )

    t0 = time.time()

    # --- 步骤 1：收集已知 UID ---
    logger.info("=" * 50)
    logger.info("步骤 1：收集已知 UID")
    logger.info("=" * 50)

    # 来源 1：meta 数据中的评分者/点评者 UID（含用户名）
    uid_map = collect_known_uid_map()          # {uid: username}
    # 来源 2：scraper 输出的 UID 文件（帖子作者 UID，无用户名）
    file_uids = collect_known_uids_from_file()  # {uid}
    # 合并：文件中的 UID 若不在 meta 中，以空用户名加入
    for uid in file_uids:
        if uid not in uid_map:
            uid_map[uid] = ""

    all_authors = collect_unique_authors()      # [username, ...]

    # 统计覆盖率
    name_to_uid: dict[str, int] = {}
    for uid, name in uid_map.items():
        if name and name not in name_to_uid:
            name_to_uid[name] = uid

    covered = sum(1 for a in all_authors if a in name_to_uid)
    logger.info("  合并后总 UID: %d 个", len(uid_map))
    logger.info("  总作者: %d 人", len(all_authors))
    logger.info("  可匹配: %d 人 (%.1f%%)", covered, 100 * covered / max(len(all_authors), 1))
    logger.info("  缺失 UID: %d 人", len(all_authors) - covered)

    if args.dry_run:
        logger.info("[dry-run] 将爬取 %d 个 UID 的资料", len(uid_map))
        if file_uids:
            logger.info("          (其中 %d 个来自 known_uids.txt)", len(file_uids))
        logger.info("[dry-run] 完成")
        return

    # --- 步骤 2：爬取已知 UID 的用户资料 ---
    target_uids = sorted(uid_map.keys())
    if args.limit:
        target_uids = target_uids[:args.limit]

    logger.info("=" * 50)
    logger.info("步骤 2：爬取用户资料（%d 个 UID）", len(target_uids))
    logger.info("=" * 50)

    writer = UsersWriter(USERS_PATH)
    writer.open()

    # 断点恢复：跳过已爬取的用户
    remaining = [uid for uid in target_uids if not writer.is_done(uid)]
    logger.info("已爬取: %d | 待爬: %d", len(target_uids) - len(remaining), len(remaining))

    session = create_session()
    success = 0
    fail = 0

    for i, uid in enumerate(remaining, start=1):
        logger.info("[%d/%d] uid=%d", i, len(remaining), uid)

        info = fetch_user_profile(session, uid)
        if info:
            # 补充用户名（如果 profile 页没解析到）
            if not info.get("username") and uid in uid_map:
                info["username"] = uid_map[uid]
            writer.write(info)
            success += 1
            logger.info("  → %s | 用户组: %s | 积分: %s | 威望: %s",
                        info.get("username", "?"),
                        info.get("user_group", "?"),
                        info.get("points", "?"),
                        info.get("prestige", "?"))
        else:
            fail += 1

        # 进度报告
        if i % 50 == 0:
            elapsed = time.time() - t0
            logger.info("  进度: %d/%d (%.0f%%) | 耗时: %s",
                        i, len(remaining), 100 * i / len(remaining),
                        format_duration(elapsed))
            writer.close()
            writer.open()  # 刷新断点

    writer.close()
    session.close()

    elapsed = time.time() - t0
    logger.info("=" * 50)
    logger.info("用户爬取完成！")
    logger.info("  成功: %d | 失败: %d | 总计: %d", success, fail, writer.count)
    logger.info("  输出: %s", USERS_PATH)
    logger.info("  耗时: %s", format_duration(elapsed))


if __name__ == "__main__":
    main()
