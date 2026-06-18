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
    create_session, fetch,
    discover_boards,
    parse_board_page, parse_board_max_page,
    parse_thread_posts, parse_thread_max_page,
    make_board_url, make_thread_url, thread_web_url,
    format_duration,
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

# 控制台 handler：强制 UTF-8（避免 Windows GBK 终端乱码）
console_handler = logging.StreamHandler(sys.stdout)
# 重写 write 方法以处理编码错误
_orig_write = console_handler.stream.write
def _safe_write(msg):
    try:
        _orig_write(msg)
    except UnicodeEncodeError:
        _orig_write(msg.encode('ascii', errors='replace').decode('ascii'))
console_handler.stream.write = _safe_write

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        console_handler,
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
    logger.info("Phase 2: 收集线程列表")
    logger.info("=" * 50)

    thread_queue: list[tuple[int, int, str, str]] = []  # (tid, fid, title, board_name)

    for fid, board_name, category in all_boards:
        if ck.is_board_complete(fid):
            logger.info("[SKIP] fid=%d (%s) 已完成，跳过", fid, board_name)
            continue

        logger.info("─" * 40)
        logger.info("收集版块: fid=%d (%s)", fid, board_name)

        # 获取第一页，同时探测总页数
        url = make_board_url(fid, 1)
        logger.info("  第 1 页: %s", url)
        if dry_run:
            logger.info("  [dry-run] 跳过下载")
            continue

        html = fetch(url, session, force_no_delay=True)

        # 解析第一页的线程
        threads_p1 = parse_board_page(html)
        max_page = parse_board_max_page(html)
        logger.info("  第 1 页: %d 个帖子 | 共 %d 页", len(threads_p1), max_page)

        for tid, title in threads_p1:
            thread_queue.append((tid, fid, title, board_name))

        # 翻页抓取
        for page in range(2, max_page + 1):
            url = make_board_url(fid, page)
            logger.info("  第 %d/%d 页: %s", page, max_page, url)
            html = fetch(url, session)
            page_threads = parse_board_page(html)
            logger.info("    → %d 个帖子", len(page_threads))
            for tid, title in page_threads:
                thread_queue.append((tid, fid, title, board_name))

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

    for idx, (tid, fid, title, board_name) in enumerate(thread_queue, start=1):
        # 从 checkpoint 判断是否跳过已处理的帖子
        last_processed = ck.get_thread_progress(fid)
        if tid <= last_processed and db.thread_exists(tid):
            continue

        cat = BOARD_CATEGORIES.get(fid, (board_name, "未分类"))[1]

        logger.info("[%d/%d] tid=%d: %s", idx, total_threads, tid, title[:60])

        try:
            # 获取帖子第一页
            url = make_thread_url(tid, 1)
            html = fetch(url, session, force_no_delay=True)

            # 检查是否是错误页（如 tid 不存在）
            if "提示信息" in html[:200] and "class=\"author\"" not in html:
                logger.warning("  → tid=%d 返回错误页，跳过", tid)
                error_count += 1
                continue

            post_pages = parse_thread_max_page(html)
            all_posts = parse_thread_posts(html)

            logger.info("  → 第 1/%d 页: %d 帖", post_pages, len(all_posts))

            # 记录每页的帖子（page → [posts]）
            posts_by_page: dict[int, list[dict]] = {1: list(all_posts)}

            # 翻页
            for page in range(2, post_pages + 1):
                url = make_thread_url(tid, page)
                html = fetch(url, session)
                page_posts = parse_thread_posts(html)
                posts_by_page[page] = page_posts
                all_posts.extend(page_posts)
                logger.info("  → 第 %d/%d 页: %d 帖", page, post_pages, len(page_posts))

            # 可选：抓取主站互动元数据
            metas_by_page: dict[int, dict[int, dict]] = {}
            if with_meta:
                for page in sorted(posts_by_page.keys()):
                    try:
                        page_metas = fetch_thread_meta(tid, page, session)
                        # 按 pid 索引
                        metas_by_page[page] = {m["pid"]: m for m in page_metas}
                        logger.debug("  → 元数据 第%d页: %d 帖", page, len(page_metas))
                    except Exception as e:
                        logger.warning("  → 元数据获取失败 第%d页: %s", page, e)
                        metas_by_page[page] = {}

            # 分类标签 → 写入（按页处理，正确计算楼层）
            for page in sorted(posts_by_page.keys()):
                for pos_in_page, post in enumerate(posts_by_page[page], start=1):
                    floor = (page - 1) * 15 + pos_in_page
                    tags_info = classify(
                        title=title,
                        content=post["content"],
                        board_name=board_name,
                    )

                    # JSONL 记录
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
                        "post_time": post["post_time"],
                        "content": post["content"],
                        "url": thread_web_url(tid),
                        **tags_info,
                    }
                    # 合并互动元数据（如果启用）
                    if with_meta:
                        page_meta = metas_by_page.get(page, {})
                        # archiver 无 pid，按楼层位置匹配（archiver 每页15帖，位置对齐）
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
                            }
                        else:
                            record["meta"] = {}

                    jsonl.write(record)

                    # DB 记录
                    db.insert_post(record)

            # 主题表记录（用首帖内容生成标签）
            thread_tags = classify(
                title=title,
                content=all_posts[0]["content"] if all_posts else "",
                board_name=board_name,
            )
            db.upsert_thread({
                "tid": tid, "fid": fid, "board": board_name, "category": cat,
                "title": title,
                "first_author": all_posts[0]["author"] if all_posts else "",
                "first_time": all_posts[0]["post_time"] if all_posts else "",
                "post_count": len(all_posts),
                "tags": thread_tags["tags"],
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

            processed += 1
            ck.update_thread_progress(fid, tid)
            ck.update_highest_tid(tid)
            ck.increment_posts(len(all_posts))

            # 批次冷却
            if processed % BATCH_SIZE == 0:
                logger.info("[WAIT] 已完成 %d/%d 个帖子，冷却 %ds ...",
                            processed, total_threads, COOLDOWN)
                time.sleep(COOLDOWN)
                ck.save()  # 每批次保存检查点

        except Exception as e:
            logger.error("[ERR] tid=%d 爬取异常: %s", tid, e, exc_info=True)
            log_failed(make_thread_url(tid), str(e))
            error_count += 1
            # 继续处理下一个，不中断
            time.sleep(COOLDOWN)  # 出错后额外冷却

    # ── 完成 ──
    elapsed = time.time() - t0
    ck.data["last_full_crawl"] = datetime.now(timezone.utc).isoformat()
    ck.save()

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

    all_boards = boards_from_discovery(session)
    new_tids: list[tuple[int, int, str, str]] = []
    updated_tids: list[tuple[int, int, str, str]] = []

    for fid, board_name, category in all_boards:
        logger.info("─" * 40)
        logger.info("扫描版块: fid=%d (%s)", fid, board_name)

        # 只检查前 2 页（新帖通常在首页）
        for page in [1, 2]:
            url = make_board_url(fid, page)
            logger.info("  第 %d 页: %s", page, url)

            if dry_run:
                page_threads = []  # skip
                continue

            html = fetch(url, session, force_no_delay=True)
            page_threads = parse_board_page(html)

            for tid, title in page_threads:
                if tid > prev_highest_tid:
                    new_tids.append((tid, fid, title, board_name))
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
                            updated_tids.append((tid, fid, title, board_name))
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

    # 复用全量爬取 Phase 3 的帖子抓取逻辑
    for idx, (tid, fid, title, board_name) in enumerate(all_to_crawl, start=1):
        cat = BOARD_CATEGORIES.get(fid, ("", "未分类"))[1]
        logger.info("[%d/%d] tid=%d: %s", idx, total, tid, title[:60])

        try:
            url = make_thread_url(tid, 1)
            html = fetch(url, session, force_no_delay=True)
            post_pages = parse_thread_max_page(html)
            all_posts = parse_thread_posts(html)

            for page in range(2, post_pages + 1):
                html = fetch(make_thread_url(tid, page), session)
                all_posts.extend(parse_thread_posts(html))

            # 增量模式下：跳过已存在的楼层
            existing_count = db.post_count_for_thread(tid)
            new_posts = all_posts[existing_count:]

            for pos, post in enumerate(all_posts, start=1):
                if pos <= existing_count:
                    continue  # 跳过已有楼层

                tags_info = classify(title, post["content"], board_name)
                record = {
                    "tid": tid, "fid": fid, "board": board_name,
                    "category": cat, "title": title,
                    "author": post["author"], "floor": pos,
                    "page": (pos - 1) // 15 + 1, "position": pos,
                    "post_time": post["post_time"],
                    "content": post["content"],
                    "url": thread_web_url(tid),
                    **tags_info,
                }
                jsonl.write(record)
                db.insert_post(record)

            db.upsert_thread({
                "tid": tid, "fid": fid, "board": board_name, "category": cat,
                "title": title,
                "first_author": all_posts[0]["author"] if all_posts else "",
                "first_time": all_posts[0]["post_time"] if all_posts else "",
                "post_count": len(all_posts),
                "last_crawl": datetime.now(timezone.utc).isoformat(),
                "url": thread_web_url(tid),
            })
            db.commit()

            ck.update_highest_tid(tid)
            ck.increment_posts(len(new_posts))

        except Exception as e:
            logger.error("❌ tid=%d 爬取异常: %s", tid, e)

    ck.data["last_full_crawl"] = datetime.now(timezone.utc).isoformat()
    ck.save()

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

        if args.full or args.dry_run:
            crawl_full(session, jsonl, db, ck,
                       fid_filter=args.fid,
                       dry_run=args.dry_run,
                       with_meta=args.with_meta)
        elif args.incremental:
            crawl_incremental(session, jsonl, db, ck,
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
