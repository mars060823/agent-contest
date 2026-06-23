#!/usr/bin/env python3
"""
bbs.casdu.cn 论坛爬虫 — 主控脚本

用法：
  # 干跑（仅收集 URL，不下载）
  python scraper.py --dry-run

  # 全量爬取
  python scraper.py --full

  # 全量爬取（单个版块测试）
  python scraper.py --full --fid 152

  # 增量爬取
  python scraper.py --incremental

  # 查看统计
  python scraper.py --info

环境变量（可选）：
  CASDU_USER  论坛登录用户名
  CASDU_PASS  论坛登录密码
"""

from __future__ import annotations

import argparse
import io
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from casdu_crawl.config import (
    BOARD_CATEGORIES,
    BATCH_SIZE, COOLDOWN,
    CASDU_USER, CASDU_PASS,
    PROJECT_ROOT,
)
from casdu_crawl.utils import (
    setup_console_utf8,
    create_session, fetch,
    discover_boards,
    parse_forumdisplay_page, parse_forumdisplay_max_page,
    parse_thread_posts, parse_thread_max_page, parse_reply_to,
    make_board_url, make_forumdisplay_url, make_thread_url, thread_web_url,
    normalize_post_time, format_duration,
    fetch_thread_meta,
)
from casdu_crawl.storage import JsonlWriter, IndexDB, Checkpoint, DEFAULT_CHECKPOINT
from casdu_crawl.classifier import classify

# ============================================================================
# 初始化
# ============================================================================

DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
JSONL_PATH = DATA_DIR / "threads.jsonl"
DB_PATH = DATA_DIR / "index.db"
CHECKPOINT_PATH = DATA_DIR / "checkpoint.json"
FAILED_LOG_PATH = LOGS_DIR / "failed_urls.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        setup_console_utf8(),
        logging.FileHandler(LOGS_DIR / "scraper.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("casdu")


# ============================================================================
# 工具
# ============================================================================

def log_failed(url: str, reason: str = ""):
    """记录失败的 URL。"""
    with FAILED_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now().isoformat()}\t{url}\t{reason}\n")
    logger.warning("记录失败 URL: %s", url)


def boards_from_discovery(session) -> list[tuple[int, str, str]]:
    """从站点动态发现版块列表，补充分类信息。

    Returns:
        [(fid, name, category), ...]
    """
    discovered = discover_boards(session)
    boards: list[tuple[int, str, str]] = []
    for fid, name in discovered:
        if fid in BOARD_CATEGORIES:
            category = BOARD_CATEGORIES[fid][1]
        else:
            category = "未分类"
            logger.info("发现未知版块: fid=%d name=%s", fid, name)
        boards.append((fid, name, category))
    return boards


# ============================================================================
# 共享：单线程处理（全量 & 增量共用）
# ============================================================================

def _process_thread_posts(
    session,
    tid: int,
    fid: int,
    title: str,
    board_name: str,
    digest: int,
    sticky: int,
    closed: int,
    jsonl: JsonlWriter,
    db: IndexDB,
    *,
    with_meta: bool = False,
    skip_existing: int = 0,
    all_uids: set[int] | None = None,
) -> tuple[int, int, bool]:
    """处理单个主题帖的全部楼层：翻页爬取、分类标签、JSONL写入、DB插入。

    全量和增量模式的共享内核。调用方只需指定 skip_existing 即可：
      - 全量模式: skip_existing=0（处理所有楼层）
      - 增量模式: skip_existing=已存在楼层数（仅处理新楼层）

    Args:
        session: 已配置的 requests.Session
        tid ~ closed: 主题帖元数据
        jsonl: JSONL 写入器
        db: SQLite 数据库
        with_meta: 是否抓取主站互动元数据
        skip_existing: 跳过的已有楼层数（0 = 全部处理）
        all_uids: 全局 UID 收集集合（原地修改）

    Returns:
        (new_posts_count, total_posts_count, error_flag)
    """
    if all_uids is None:
        all_uids = set()

    cat = BOARD_CATEGORIES.get(fid, (board_name, "未分类"))[1]

    # ── 翻页抓取 ──
    url = make_thread_url(tid, 1)
    html = fetch(url, session, force_no_delay=True)

    if "提示信息" in html[:200] and "class=\"author\"" not in html:
        logger.warning("  → tid=%d 返回错误页，跳过", tid)
        return 0, 0, True

    post_pages = parse_thread_max_page(html)
    all_posts = parse_thread_posts(html)
    posts_by_page: dict[int, list[dict]] = {1: list(all_posts)}

    logger.info("  → 第 1/%d 页: %d 帖", post_pages, len(all_posts))

    for page in range(2, post_pages + 1):
        html = fetch(make_thread_url(tid, page), session)
        page_posts = parse_thread_posts(html)
        posts_by_page[page] = page_posts
        all_posts.extend(page_posts)
        logger.info("  → 第 %d/%d 页: %d 帖", page, post_pages, len(page_posts))

    new_post_count = 0
    total_post_count = len(all_posts)

    # ── 元数据抓取（按需） ──
    metas_by_page: dict[int, dict[int, dict]] = {}
    if with_meta:
        if skip_existing == 0:
            # 全量模式：所有页都抓元数据
            pages_to_fetch = sorted(posts_by_page.keys())
        else:
            # 增量模式：仅抓取包含新楼层的页
            pages_to_fetch = sorted({
                (pos - 1) // 15 + 1
                for pos in range(skip_existing + 1, total_post_count + 1)
            })

        for page in pages_to_fetch:
            try:
                page_metas = fetch_thread_meta(tid, page, session)
                metas_by_page[page] = {m["pid"]: m for m in page_metas}
                logger.debug("  → 元数据 第%d页: %d 帖", page, len(page_metas))
            except Exception as e:
                logger.warning("  → 元数据获取失败 第%d页: %s", page, e)
                metas_by_page[page] = {}

    # ── 按页处理楼层 ──
    for page in sorted(posts_by_page.keys()):
        for pos_in_page, post in enumerate(posts_by_page[page], start=1):
            floor = (page - 1) * 15 + pos_in_page
            if floor <= skip_existing:
                continue

            new_post_count += 1

            # 分类标签
            tags_info = classify(
                title=title,
                content=post["content"],
                board_name=board_name,
            )
            if sticky:
                tags_info.setdefault("tags", [])
                if "置顶" not in tags_info["tags"]:
                    tags_info["tags"].insert(0, "置顶")

            # 回复引用
            reply_to_floor, reply_to_user = parse_reply_to(post["content"])

            # 构建记录
            record = {
                "tid": tid,
                "fid": fid,
                "board": board_name,
                "category": cat,
                "title": title,
                "author": post["author"],
                "floor": floor,
                "page": page,
                "position": pos_in_page,
                "post_time": normalize_post_time(post["post_time"]),
                "content": post["content"],
                "content_len": len(post["content"]),
                "thread_total_floors": total_post_count,
                "url": thread_web_url(tid),
                "digest": digest,
                "sticky": sticky,
                "closed": closed,
                "reply_to_floor": reply_to_floor,
                "reply_to_user": reply_to_user or "",
                **tags_info,
            }

            # 管理标记
            if digest:
                record["tags"].append("精华")
            if sticky:
                record["tags"].append("置顶")
            if closed:
                record["tags"].append("关闭")

            # 互动元数据匹配
            if with_meta:
                page_meta = metas_by_page.get(page, {})
                matched = None
                if pos_in_page <= len(page_meta):
                    meta_list = list(page_meta.values())
                    matched = meta_list[pos_in_page - 1]
                if matched:
                    record["meta"] = {
                        "rating_count": matched.get("rating_count", 0),
                        "rating_coins": matched.get("rating_coins", 0),
                        "rating_details": matched.get("rating_details", []),
                        "recommend_add": matched.get("recommend_add", 0),
                        "recommend_subtract": matched.get("recommend_subtract", 0),
                        "favorite_count": matched.get("favorite_count", 0),
                        "comment_count": matched.get("comment_count", 0),
                        "comments": matched.get("comments", []),
                        "author_uid": matched.get("author_uid"),
                    }
                    record["real_pid"] = matched.get("pid")
                    if matched.get("author_uid"):
                        all_uids.add(matched["author_uid"])
                    for r in matched.get("rating_details", []):
                        if r.get("uid"):
                            all_uids.add(r["uid"])
                    for c in matched.get("comments", []):
                        if c.get("uid"):
                            all_uids.add(c["uid"])
                    db.insert_ratings(
                        tid, floor, matched.get("pid"),
                        matched.get("rating_details", []),
                    )
                else:
                    record["meta"] = {}

            jsonl.write(record)
            db.insert_post(record)

    # ── 主题表 upsert ──
    thread_tags = classify(
        title=title,
        content=all_posts[0]["content"] if all_posts else "",
        board_name=board_name,
    )
    thread_tags_list = list(thread_tags["tags"])
    if digest:
        thread_tags_list.append("精华")
    if sticky:
        thread_tags_list.append("置顶")
    if closed:
        thread_tags_list.append("关闭")
    db.upsert_thread({
        "tid": tid, "fid": fid, "board": board_name, "category": cat,
        "title": title,
        "first_author": all_posts[0]["author"] if all_posts else "",
        "first_time": all_posts[0]["post_time"] if all_posts else "",
        "post_count": total_post_count,
        "digest": digest,
        "sticky": sticky,
        "closed": closed,
        "tags": thread_tags_list,
        "year": thread_tags["year"],
        "season": thread_tags["season"],
        "event_type": thread_tags["event_type"],
        "routes": thread_tags["routes"],
        "roles": thread_tags["roles"],
        "problems": thread_tags["problems"],
        "last_crawl": datetime.now(timezone.utc).isoformat(),
        "url": thread_web_url(tid),
    })
    db.commit()

    return new_post_count, total_post_count, False


# ============================================================================
# 全量爬取
# ============================================================================

def crawl_full(
    session,
    jsonl: JsonlWriter,
    db: IndexDB,
    ck: Checkpoint,
    *,
    fid_filter: int = 0,
    dry_run: bool = False,
    with_meta: bool = False,
):
    """全量爬取主流程。

    1. 发现所有版块
    2. 逐版块收集线程列表
    3. 逐帖爬取回帖内容
    """
    t0 = time.time()

    # UID 收集集合（用于用户信息爬取）
    all_uids: set[int] = set()

    # ── Phase 1: 版块发现 ──
    logger.info("=" * 50)
    logger.info("Phase 1: 发现版块")
    logger.info("=" * 50)
    all_boards = boards_from_discovery(session)

    if fid_filter:
        all_boards = [(fid_filter, n, c) for fid, n, c in all_boards
                      if fid == fid_filter]
        if not all_boards:
            logger.error("未找到版块 fid=%d", fid_filter)
            return

    logger.info("共 %d 个版块待处理", len(all_boards))

    # ── Phase 2: 收集线程列表 ──
    logger.info("=" * 50)
    logger.info("Phase 2: 收集线程列表 (forumdisplay.php)")
    logger.info("=" * 50)

    # (tid, fid, title, board_name, digest, sticky, closed)
    thread_queue: list[tuple[int, int, str, str, int, int, int]] = []
    seen_global_stickies: set[int] = set()  # 全局/分区置顶帖去重

    for fid, board_name, category in all_boards:
        if ck.is_board_complete(fid):
            logger.info("[SKIP] fid=%d (%s) 已完成，跳过", fid, board_name)
            continue

        logger.info("─" * 40)
        logger.info("收集版块: fid=%d (%s)", fid, board_name)

        # 获取第一页，同时探测总页数
        url = make_forumdisplay_url(fid, 1)
        logger.info("  第 1 页: %s", url)
        if dry_run:
            logger.info("  [dry-run] 跳过下载")
            continue

        html = fetch(url, session, force_no_delay=True)

        # 解析第一页的线程 + 元数据
        fd_meta = parse_forumdisplay_page(html)
        max_page = parse_forumdisplay_max_page(html)
        logger.info("  第 1 页: %d 个帖子 | 共 %d 页", len(fd_meta), max_page)

        sticky_skipped = 0
        for tid, meta in fd_meta.items():
            sticky = meta.get("sticky", 0)
            # 全局置顶(sticky=3)/分区置顶(sticky=2) 去重：先到先得
            if sticky >= 2:
                if tid in seen_global_stickies:
                    sticky_skipped += 1
                    continue
                seen_global_stickies.add(tid)
            thread_queue.append((
                tid, fid, meta.get("title", ""), board_name,
                meta.get("digest", 0),
                sticky,
                meta.get("closed", 0),
            ))
        if sticky_skipped:
            logger.info("    → 跳过 %d 个已收录的全局/分区置顶帖", sticky_skipped)

        # 翻页抓取
        for page in range(2, max_page + 1):
            url = make_forumdisplay_url(fid, page)
            logger.info("  第 %d/%d 页: %s", page, max_page, url)
            html = fetch(url, session)
            page_meta = parse_forumdisplay_page(html)
            logger.info("    → %d 个帖子", len(page_meta))
            for tid, meta in page_meta.items():
                sticky = meta.get("sticky", 0)
                if sticky >= 2:
                    if tid in seen_global_stickies:
                        continue
                    seen_global_stickies.add(tid)
                thread_queue.append((
                    tid, fid, meta.get("title", ""), board_name,
                    meta.get("digest", 0),
                    sticky,
                    meta.get("closed", 0),
                ))

        # 版块完成，写检查点
        ck.mark_board_complete(fid)
        logger.info("[OK] fid=%d 线程列表收集完成，当前队列共 %d 个主题",
                    fid, len(thread_queue))

    logger.info("Phase 2 完成: 共 %d 个主题帖待爬取", len(thread_queue))
    if dry_run:
        elapsed = time.time() - t0
        estimated = len(thread_queue) * 2.0 + 60  # 每帖约 2 秒 + 版块开销
        logger.info("[dry-run] 预计全量耗时: %s", format_duration(estimated))
        logger.info("[dry-run] 完成")
        return

    # ── Phase 3: 爬取帖子内容 ──
    logger.info("=" * 50)
    logger.info("Phase 3: 爬取帖子内容")
    logger.info("=" * 50)

    total_threads = len(thread_queue)
    processed = 0
    error_count = 0

    for idx, (tid, fid, title, board_name, digest, sticky, closed) in enumerate(thread_queue, start=1):
        # 从 checkpoint 判断是否跳过已处理的帖子
        last_processed = ck.get_thread_progress(fid)
        if tid <= last_processed and db.thread_exists(tid):
            continue

        logger.info("[%d/%d] tid=%d: %s", idx, total_threads, tid, title[:60])

        new_count, total_count, err = _process_thread_posts(
            session, tid, fid, title, board_name, digest, sticky, closed,
            jsonl, db,
            with_meta=with_meta,
            all_uids=all_uids,
        )

        if err:
            error_count += 1
            log_failed(make_thread_url(tid))
            time.sleep(COOLDOWN)
            continue

        processed += 1
        ck.update_thread_progress(fid, tid)
        ck.update_highest_tid(tid)
        ck.increment_posts(total_count)

        # 批次冷却
        if processed % BATCH_SIZE == 0:
            logger.info("[WAIT] 已完成 %d/%d 个帖子，冷却 %ds ...",
                        processed, total_threads, COOLDOWN)
            time.sleep(COOLDOWN)
            ck.save()

    # ── 完成 ──
    elapsed = time.time() - t0
    ck.data["last_full_crawl"] = datetime.now(timezone.utc).isoformat()
    ck.save()

    # 写入 UID 集合文件
    if all_uids:
        uid_path = DATA_DIR / "known_uids.txt"
        with uid_path.open("w", encoding="utf-8") as f:
            for uid in sorted(all_uids):
                f.write(f"{uid}\n")
        logger.info("  收集 UID: %d 个 → %s", len(all_uids), uid_path.name)

    logger.info("=" * 50)
    logger.info("全量爬取完成！")
    logger.info("  总耗时: %s", format_duration(elapsed))
    logger.info("  处理主题: %d", processed)
    logger.info("  归档帖子: %d", ck.data["total_posts_archived"])
    logger.info("  失败数: %d", error_count)
    logger.info("  输出: %s", JSONL_PATH)


# ============================================================================
# 增量爬取
# ============================================================================

def crawl_incremental(
    session,
    jsonl: JsonlWriter,
    db: IndexDB,
    ck: Checkpoint,
    *,
    dry_run: bool = False,
    with_meta: bool = False,
):
    """增量爬取主流程。

    1. 读取上次全量的最大 tid
    2. 对每个版块，只爬首页+第2页，发现新 tid
    3. 对旧 tid，检查是否有新回复
    """
    t0 = time.time()

    if not ck.load():
        logger.error("没有检查点文件，请先执行全量爬取 (--full)")
        return

    prev_highest_tid = ck.data["highest_tid_seen"]
    logger.info("上次全量最大 tid: %d", prev_highest_tid)

    all_uids: set[int] = set()

    all_boards = boards_from_discovery(session)
    # (tid, fid, title, board_name, digest, sticky, closed)
    new_tids: list[tuple[int, int, str, str, int, int, int]] = []
    updated_tids: list[tuple[int, int, str, str, int, int, int]] = []

    for fid, board_name, category in all_boards:
        logger.info("─" * 40)
        logger.info("扫描版块: fid=%d (%s)", fid, board_name)

        # 只检查前 2 页（新帖通常在首页）
        for page in [1, 2]:
            url = make_forumdisplay_url(fid, page)
            logger.info("  第 %d 页: %s", page, url)

            if dry_run:
                continue

            html = fetch(url, session, force_no_delay=True)
            fd_meta = parse_forumdisplay_page(html)

            for tid, meta in fd_meta.items():
                title = meta.get("title", "")
                digest = meta.get("digest", 0)
                sticky = meta.get("sticky", 0)
                closed = meta.get("closed", 0)
                if tid > prev_highest_tid:
                    new_tids.append((tid, fid, title, board_name, digest, sticky, closed))
                else:
                    # 旧帖：检查是否有新回复
                    existing_count = db.post_count_for_thread(tid)
                    # 快速探测：只爬第一页比较
                    try:
                        post_url = make_thread_url(tid, 1)
                        post_html = fetch(post_url, session, force_no_delay=True)
                        current_posts = parse_thread_posts(post_html)
                        current_page_count = parse_thread_max_page(post_html)

                        # 估算总帖数
                        if current_page_count > 1:
                            # 需要翻页探测最后一页
                            last_url = make_thread_url(tid, current_page_count)
                            last_html = fetch(last_url, session)
                            last_page_posts = parse_thread_posts(last_html)
                            total_current = (current_page_count - 1) * 15 + len(last_page_posts)
                        else:
                            total_current = len(current_posts)

                        if total_current > existing_count:
                            updated_tids.append((tid, fid, title, board_name, digest, sticky, closed))
                            logger.info("  → tid=%d 有新回复 (%d→%d)",
                                        tid, existing_count, total_current)
                    except Exception as e:
                        logger.warning("  → tid=%d 探测异常: %s", tid, e)

    logger.info("新主题: %d | 有更新: %d", len(new_tids), len(updated_tids))

    if dry_run:
        logger.info("[dry-run] 完成")
        return

    # 合并待爬列表（新帖优先）
    all_to_crawl = new_tids + updated_tids
    total = len(all_to_crawl)

    for idx, (tid, fid, title, board_name, digest, sticky, closed) in enumerate(all_to_crawl, start=1):
        logger.info("[%d/%d] tid=%d: %s", idx, total, tid, title[:60])

        existing_count = db.post_count_for_thread(tid)
        new_count, total_count, err = _process_thread_posts(
            session, tid, fid, title, board_name, digest, sticky, closed,
            jsonl, db,
            with_meta=with_meta,
            skip_existing=existing_count,
            all_uids=all_uids,
        )

        if err:
            continue

        ck.update_highest_tid(tid)
        ck.increment_posts(new_count)

    ck.data["last_full_crawl"] = datetime.now(timezone.utc).isoformat()
    ck.save()

    # 写入 UID 集合文件
    if all_uids:
        uid_path = DATA_DIR / "known_uids.txt"
        with uid_path.open("w", encoding="utf-8") as f:
            for uid in sorted(all_uids):
                f.write(f"{uid}\n")
        logger.info("  收集 UID: %d 个 → %s", len(all_uids), uid_path.name)

    elapsed = time.time() - t0
    logger.info("=" * 50)
    logger.info("增量爬取完成！耗时: %s", format_duration(elapsed))


# ============================================================================
# 系统信息
# ============================================================================

def show_info():
    """显示当前系统状态。"""
    print("=" * 50)
    print("bbs.casdu.cn 爬虫 — 系统信息")
    print("=" * 50)
    print()

    # 检查点
    ck = Checkpoint(CHECKPOINT_PATH)
    if ck.load():
        print("📋 上次爬取: ", ck.data.get("last_full_crawl", "未知"))
        print("📊 最高 tid: ", ck.data["highest_tid_seen"])
        print("📝 已归档帖: ", ck.data["total_posts_archived"])
        completed = sum(1 for v in ck.data["completed_boards"].values() if v)
        total = len(ck.data["completed_boards"])
        print(f"📁 完成版块: {completed}/{total}")
    else:
        print("⚠ 尚未开始爬取（无检查点）")

    print()

    # 数据库
    db = IndexDB(DB_PATH)
    if DB_PATH.exists():
        try:
            db.open()
            thread_count = db.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
            post_count = db.conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
            db.close()
            print(f"🗄 数据库: {thread_count} 主题帖, {post_count} 回帖")
        except Exception as e:
            print(f"⚠ 数据库读取异常: {e}")
    else:
        print("🗄 数据库: 未创建")

    print()

    # 登录状态
    if CASDU_USER:
        print(f"🔑 登录凭据: 已设置 (用户: {CASDU_USER})")
    else:
        print("🔑 登录凭据: 未设置（匿名模式）")


# ============================================================================
# 命令行入口
# ============================================================================

def main():
    # 强制 UTF-8 输出 —— 解决 Windows GBK 终端乱码
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="bbs.casdu.cn 论坛爬虫",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python scraper.py --dry-run           # 干跑：收集 URL 列表
  python scraper.py --full              # 全量爬取
  python scraper.py --full --fid 152    # 单版块测试
  python scraper.py --incremental       # 增量更新
  python scraper.py --info              # 查看状态
        """,
    )
    parser.add_argument("--full", action="store_true",
                        help="全量爬取模式")
    parser.add_argument("--incremental", action="store_true",
                        help="增量爬取模式")
    parser.add_argument("--dry-run", action="store_true",
                        help="干跑模式：仅收集 URL，不实际下载")
    parser.add_argument("--fid", type=int, default=0,
                        help="限定版块 ID（用于测试）")
    parser.add_argument("--with-meta", action="store_true",
                        help="抓取主站互动元数据（评分/支持反对/收藏/点评），耗时增加")
    parser.add_argument("--info", action="store_true",
                        help="显示系统状态")
    parser.add_argument("--quiet", "-q", action="store_true",
                        help="减少日志输出")

    args = parser.parse_args()

    if args.quiet:
        logging.getLogger("casdu").setLevel(logging.WARNING)

    # --info 模式
    if args.info:
        show_info()
        return

    # 初始化
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    session = create_session()
    jsonl = JsonlWriter(JSONL_PATH)
    db = IndexDB(DB_PATH)
    ck = Checkpoint(CHECKPOINT_PATH)
    ck.load()

    try:
        jsonl.open()
        db.open()

        if args.incremental:
            crawl_incremental(session, jsonl, db, ck,
                              dry_run=args.dry_run,
                              with_meta=args.with_meta)
        elif args.full or args.dry_run:
            crawl_full(session, jsonl, db, ck,
                       fid_filter=args.fid,
                       dry_run=args.dry_run,
                       with_meta=args.with_meta)
        else:
            parser.print_help()
    finally:
        jsonl.close()
        db.close()
        session.close()


if __name__ == "__main__":
    main()
