#!/usr/bin/env python3
"""
author_uid 补全脚本

从 users.jsonl 读取 username→uid 映射，回填 threads.jsonl 中缺失的 author_uid 字段。

使用场景：
  不加 --with-meta 爬取时，JSONL 只有 author（用户名）没有 author_uid。
  运行 crawl_users.py 获取用户资料后，运行本脚本补全 UID。

用法：
    python scripts/enrich_uids.py                           # 补全 data/threads.jsonl
    python scripts/enrich_uids.py --input other.jsonl       # 指定输入
    python scripts/enrich_uids.py --dry-run                 # 仅统计，不写入

特性：
  - 幂等：已有 author_uid 的记录不覆盖
  - 容错：无法匹配的用户设为 null，不中断
  - 原子写入：先写临时文件再 rename，避免中断导致数据损坏
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
DATA_DIR = PROJECT_DIR / "data"

USERS_PATH = DATA_DIR / "users.jsonl"
THREADS_PATH = DATA_DIR / "threads.jsonl"


def load_uid_map(users_path: Path) -> dict[str, int]:
    """从 users.jsonl 读取 username → uid 映射。

    同名用户取最后出现的记录（以最后爬取到的为准）。
    """
    uid_map: dict[str, int] = {}
    if not users_path.exists():
        print(f"错误：用户文件不存在: {users_path}")
        print("请先运行 crawl_users.py 爬取用户资料")
        sys.exit(1)

    with users_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            username = rec.get("username", "").strip()
            uid = rec.get("uid")
            if username and uid is not None:
                uid_map[username] = uid

    print(f"已加载 {len(uid_map)} 个 username→uid 映射")
    return uid_map


def enrich(input_path: Path, uid_map: dict[str, int], dry_run: bool = False):
    """逐行读取 JSONL，补全 author_uid 后写回。"""
    if not input_path.exists():
        print(f"错误：输入文件不存在: {input_path}")
        sys.exit(1)

    total = 0
    already_has = 0
    filled = 0
    unmatched = 0
    unknown_authors: set[str] = set()

    # 先读到内存（JSONL 每行一条，体积可控）
    records: list[dict] = []
    with input_path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            total += 1

            if "author_uid" in rec and rec["author_uid"] is not None:
                already_has += 1
                records.append(rec)
                continue

            author = rec.get("author", "").strip()
            if author and author in uid_map:
                rec["author_uid"] = uid_map[author]
                filled += 1
            else:
                rec["author_uid"] = None
                unmatched += 1
                if author:
                    unknown_authors.add(author)

            records.append(rec)

    print(f"总记录: {total}")
    print(f"  已有 author_uid: {already_has}")
    print(f"  本次补全:        {filled}")
    print(f"  无法匹配:        {unmatched}")

    if unknown_authors:
        sample = sorted(unknown_authors)[:10]
        print(f"  未知作者 ({len(unknown_authors)} 人): {', '.join(sample)}"
              + (" ..." if len(unknown_authors) > 10 else ""))

    if dry_run:
        print("[dry-run] 未写入文件")
        return

    # 原子写入：临时文件 → rename
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=input_path.parent,
        prefix=".threads_",
        suffix=".jsonl",
    )
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            for rec in records:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp_path, input_path)
        print(f"已写入: {input_path}")
    except Exception:
        os.unlink(tmp_path)
        raise


def main():
    parser = argparse.ArgumentParser(
        description="补全 threads.jsonl 中缺失的 author_uid"
    )
    parser.add_argument("--input", "-i",
                        default=str(THREADS_PATH),
                        help=f"输入 JSONL 文件（默认: {THREADS_PATH}）")
    parser.add_argument("--users",
                        default=str(USERS_PATH),
                        help=f"用户资料文件（默认: {USERS_PATH}）")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅统计，不写入")
    args = parser.parse_args()

    uid_map = load_uid_map(Path(args.users))
    if not uid_map:
        print("错误：未获取到任何 username→uid 映射")
        sys.exit(1)

    enrich(Path(args.input), uid_map, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
