#!/usr/bin/env python3
"""
Demo 抽样爬取脚本

爬取所有置顶帖 + 各版块随机 3 帖，保存到 data/demo/。
用于快速获取论坛样本数据，验证分类器和数据管线。

用法：
    python scripts/run_demo.py
    python scripts/run_demo.py --samples 5          # 每版块抽 5 帖
    python scripts/run_demo.py --fid 2              # 只抽指定版块
"""

import argparse
import io
import json
import logging
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 强制 UTF-8 输出 —— 解决 Windows GBK 终端乱码
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from casdu_crawl.config import (
    BOARD_CATEGORIES,
    PROJECT_ROOT,
)
from casdu_crawl.utils import (
    setup_console_utf8,
    create_session, fetch,
    discover_boards, parse_board_page, parse_board_max_page,
    parse_thread_posts, parse_thread_max_page, parse_reply_to,
    make_board_url, make_thread_url, thread_web_url,
    normalize_post_time, format_duration,
)
from casdu_crawl.storage import JsonlWriter
from casdu_crawl.classifier import classify

# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------
DEMO_DIR = PROJECT_ROOT / "data" / "demo"
JSONL_PATH = DEMO_DIR / "threads.jsonl"
SUMMARY_PATH = DEMO_DIR / "summary.json"

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[setup_console_utf8()],
)
logger = logging.getLogger("casdu-demo")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
STICKY_THRESHOLD = 3  # 同一 tid 出现在 ≥N 个版块 → 全局置顶


# ---------------------------------------------------------------------------
# 版块扫描
# ---------------------------------------------------------------------------

def collect_board_threads(session, fid_filter: int = 0):
    """扫描所有版块的前两页线程列表。

    Returns:
        tid_fids:    tid → [fid, ...]   跨版块出现记录
        board_tids:  fid → [(tid, title), ...]
        board_names: fid → board_name
    """
    discovered = discover_boards(session)
    boards = [(fid, name) for fid, name in discovered
              if not fid_filter or fid == fid_filter]

    if not boards:
        logger.error("未发现任何版块")
        return {}, {}, {}

    logger.info("发现 %d 个版块，开始扫描...", len(boards))

    tid_fids: dict[int, list[int]] = {}
    board_tids: dict[int, list[tuple[int, str]]] = {}
    board_names: dict[int, str] = {}

    for fid, name in boards:
        logger.info("扫描版块: fid=%d (%s)", fid, name)
        board_names[fid] = name

        # 扫前两页（置顶帖可能在第二页头几条）
        all_threads: list[tuple[int, str]] = []
        for page in [1, 2]:
            url = make_board_url(fid, page)
            try:
                html = fetch(url, session, force_no_delay=(page == 1))
            except Exception as e:
                logger.warning("  → 第%d页获取失败: %s", page, e)
                continue

            # 先解析当前页线程（无论第几页都要解析）
            threads = parse_board_page(html)
            all_threads.extend(threads)

            if page == 1:
                max_page = parse_board_max_page(html)
                if max_page < 2:
                    # 只有一页，解析完就退出，不翻第 2 页
                    for tid, title in threads:
                        if tid not in tid_fids:
                            tid_fids[tid] = []
                        tid_fids[tid].append(fid)
                    break

            for tid, title in threads:
                if tid not in tid_fids:
                    tid_fids[tid] = []
                tid_fids[tid].append(fid)

        board_tids[fid] = all_threads
        logger.info("  → %d 个主题", len(all_threads))

    logger.info("扫描完成: %d 个版块, %d 个不同 tid",
                len(board_tids), len(tid_fids))
    return tid_fids, board_tids, board_names


# ---------------------------------------------------------------------------
# 置顶帖检测
# ---------------------------------------------------------------------------

def detect_sticky(tid_fids: dict[int, list[int]]) -> set[int]:
    """检测全局置顶帖：同一 tid 出现在 ≥ STICKY_THRESHOLD 个版块。"""
    global_sticky: set[int] = set()
    for tid, fids in tid_fids.items():
        if len(set(fids)) >= STICKY_THRESHOLD:
            global_sticky.add(tid)
    logger.info("检测到 %d 个全局置顶帖", len(global_sticky))
    return global_sticky


# ---------------------------------------------------------------------------
# 帖子爬取
# ---------------------------------------------------------------------------

def crawl_threads(
    session,
    samples: list[tuple[int, int, str, str, bool]],
) -> list[dict]:
    """爬取抽样帖子的全部楼层内容，返回 JSONL 记录列表。

    Args:
        samples: [(tid, fid, title, board_name, is_sticky), ...]

    Returns:
        扁平化的 JSONL 记录列表（每层楼一行）
    """
    total = len(samples)
    records: list[dict] = []

    for idx, (tid, fid, title, board_name, is_sticky) in enumerate(samples, start=1):
        sticky_flag = "[置顶] " if is_sticky else ""
        logger.info("[%d/%d] tid=%d: %s%s", idx, total, tid, sticky_flag, title[:60])

        try:
            # 获取第一页
            url = make_thread_url(tid, 1)
            html = fetch(url, session, force_no_delay=True)

            if "提示信息" in html[:200] and "class=\"author\"" not in html:
                logger.warning("  → tid=%d 返回错误页，跳过", tid)
                continue

            all_posts = parse_thread_posts(html)
            max_page = parse_thread_max_page(html)
            logger.info("  → 第 1/%d 页: %d 帖", max_page, len(all_posts))

            # 翻页
            for page in range(2, max_page + 1):
                url_p = make_thread_url(tid, page)
                html_p = fetch(url_p, session)
                page_posts = parse_thread_posts(html_p)
                all_posts.extend(page_posts)
                logger.info("  → 第 %d/%d 页: %d 帖", page, max_page, len(page_posts))

            # 分类标签 → 生成记录
            for floor, post in enumerate(all_posts, start=1):
                tags_info = classify(
                    title=title,
                    content=post.get("content", ""),
                    board_name=board_name,
                )

                # 提取楼层回复引用
                reply_to_floor, reply_to_user = parse_reply_to(post.get("content", ""))

                record = {
                    "tid": tid,
                    "fid": fid,
                    "board": board_name,
                    "category": BOARD_CATEGORIES.get(fid, (board_name, "未分类"))[1],
                    "title": title,
                    "author": post.get("author", ""),
                    "floor": floor,
                    "page": (floor - 1) // 15 + 1,
                    "position": ((floor - 1) % 15) + 1,
                    "post_time": normalize_post_time(post.get("post_time", "")),
                    "content": post.get("content", ""),
                    "content_len": len(post.get("content", "")),
                    "thread_total_floors": len(all_posts),
                    "url": thread_web_url(tid),
                    "digest": 0,
                    "sticky": 2 if is_sticky else 0,
                    "closed": 0,
                    "reply_to_floor": reply_to_floor,
                    "reply_to_user": reply_to_user or "",
                    "meta": {},
                    **tags_info,
                }
                records.append(record)

        except Exception as e:
            logger.error("  → tid=%d 爬取异常: %s", tid, e, exc_info=True)

    return records


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Demo 抽样爬取 — 置顶帖 + 每版块随机 N 帖"
    )
    parser.add_argument("--samples", "-n", type=int, default=3,
                        help="每版块随机抽取的普通帖数（默认 3）")
    parser.add_argument("--fid", type=int, default=0,
                        help="限定版块 ID（用于测试）")
    parser.add_argument("--seed", type=int, default=42,
                        help="随机种子（默认 42，可复现）")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    logger.info("=" * 50)
    logger.info("Demo 抽样爬取")
    logger.info("  每版块样本数: %d", args.samples)
    logger.info("  输出目录: %s", DEMO_DIR)
    logger.info("=" * 50)

    session = create_session()

    try:
        # 1. 扫描版块
        tid_fids, board_tids, board_names = collect_board_threads(
            session, fid_filter=args.fid
        )
        if not board_tids:
            logger.error("未获取到任何版块数据，退出")
            return

        # 2. 检测置顶帖
        global_sticky = detect_sticky(tid_fids)

        # 3. 选取抽样线程
        # 置顶帖（每个 tid 只收录一次，归入首个出现的版块）
        sticky_samples: list[tuple[int, int, str, str, bool]] = []
        seen_sticky: set[int] = set()
        for tid in sorted(global_sticky):
            for fid, threads in board_tids.items():
                # threads 是 [(tid, title), ...]，t 是 tid
                match = next(((t, title) for t, title in threads if t == tid), None)
                if match and tid not in seen_sticky:
                    _, title = match
                    sticky_samples.append((
                        tid, fid, title, board_names.get(fid, ""), True
                    ))
                    seen_sticky.add(tid)
                    break

        logger.info("全局置顶帖: %d 个", len(sticky_samples))

        # 普通帖（每版块随机 N 个，排除置顶帖）
        normal_samples: list[tuple[int, int, str, str, bool]] = []
        for fid, threads in board_tids.items():
            normal = [(tid, title) for tid, title in threads
                      if tid not in global_sticky]
            n = min(args.samples, len(normal))
            picks = rng.sample(normal, n) if n > 0 else []
            bname = board_names.get(fid, "")
            for tid, title in picks:
                normal_samples.append((tid, fid, title, bname, False))

        all_samples = sticky_samples + normal_samples
        logger.info("抽样总计: %d 置顶 + %d 普通 = %d 个帖子",
                    len(sticky_samples), len(normal_samples), len(all_samples))

        if not all_samples:
            logger.warning("无帖子可爬取")
            return

        # 4. 爬取帖子内容
        logger.info("=" * 50)
        logger.info("开始爬取 %d 个帖子...", len(all_samples))
        logger.info("=" * 50)

        records = crawl_threads(session, all_samples)

        # 5. 写入 JSONL
        jsonl = JsonlWriter(JSONL_PATH)
        jsonl.open()
        for rec in records:
            jsonl.write(rec)
        jsonl.close()

        # 6. 写入摘要
        sampled_tids = sorted(set(tid for tid, _, _, _, _ in all_samples))
        summary = {
            "created": datetime.now(timezone.utc).isoformat(),
            "command": f"run_demo.py --samples {args.samples} --seed {args.seed}"
                       + (f" --fid {args.fid}" if args.fid else ""),
            "samples_per_board": args.samples,
            "seed": args.seed,
            "boards_scanned": len(board_tids),
            "sticky_threads": len(sticky_samples),
            "normal_threads": len(normal_samples),
            "total_threads": len(all_samples),
            "total_posts": len(records),
            "sticky_tids": sorted(seen_sticky),
            "sampled_tids": sampled_tids,
            "per_board": {
                str(fid): {
                    "name": board_names.get(fid, ""),
                    "total_threads": len(threads),
                    "sampled": sum(1 for _, f, _, _, _ in all_samples if f == fid),
                }
                for fid, threads in board_tids.items()
            },
        }
        with SUMMARY_PATH.open("w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)

        elapsed = time.time() - t0
        logger.info("=" * 50)
        logger.info("完成！")
        logger.info("  耗时: %s", format_duration(elapsed))
        logger.info("  帖子数: %d 主题 → %d 条记录", len(all_samples), len(records))
        logger.info("  输出: %s", JSONL_PATH)
        logger.info("  摘要: %s", SUMMARY_PATH)

    finally:
        session.close()


if __name__ == "__main__":
    main()
