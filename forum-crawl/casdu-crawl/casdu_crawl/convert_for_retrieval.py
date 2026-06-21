#!/usr/bin/env python3
"""
格式转换脚本

将 scraper 输出的 threads.jsonl 转换为检索管线可用的 faiss_meta.jsonl 同构格式。

用法：
    python convert_for_retrieval.py                     # 默认输入 data/threads.jsonl
    python convert_for_retrieval.py --input other.jsonl
    python convert_for_retrieval.py --max-chars 1500 --overlap 200

输出：
    data/casdu_meta.jsonl    → 可直接喂给 embedding 管线
"""

import argparse
import io
import json
import re
import sys
import time
from pathlib import Path

from casdu_crawl.config import PROJECT_ROOT

DATA_DIR = PROJECT_ROOT / "data"


def _safe_dumps(obj) -> str:
    """json.dumps 的安全封装，自动清理 GBK lone surrogate。"""
    data = _sanitize(obj)
    return json.dumps(data, ensure_ascii=False)


def _sanitize(obj):
    """递归清理字符串中的 lone surrogate 字符。"""
    if isinstance(obj, str):
        return obj.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


# ============================================================================
# 文本清洗 + 分块
# ============================================================================

def normalize_text(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("　", " ").replace("�", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_text(text: str, max_chars: int = 1500, overlap: int = 200) -> list[str]:
    """语义感知分块：在句末标点处切分，贪婪打包到 max_chars。"""
    if len(text) <= max_chars:
        return [text]

    sentences = re.split(r'(?<=[。？！.?!])\s*', text)
    sentences = [s for s in sentences if s.strip()]

    if not sentences:
        chunks: list[str] = []
        start = 0
        step = max(1, max_chars - overlap)
        while start < len(text):
            chunk = text[start: start + max_chars].strip()
            if chunk:
                chunks.append(chunk)
            start += step
        return chunks

    chunks: list[str] = []
    current = ""

    for sentence in sentences:
        if len(sentence) > max_chars:
            if current.strip():
                chunks.append(current.strip())
                current = ""
            start = 0
            step = max(1, max_chars - overlap)
            while start < len(sentence):
                part = sentence[start: start + max_chars].strip()
                if part:
                    chunks.append(part)
                start += step
            if chunks and overlap > 0:
                last = chunks[-1]
                current = last[-overlap:] if len(last) > overlap else last
            continue

        if current and len(current) + len(sentence) > max_chars:
            chunks.append(current.strip())
            if overlap > 0 and len(current) > overlap:
                current = current[-overlap:] + sentence
            else:
                current = sentence
        else:
            current += sentence

    if current.strip():
        chunks.append(current.strip())

    return chunks


# ============================================================================
# cues + source_label — 语义线索分析
# ============================================================================

def cues(title: str, text: str) -> str:
    """分析帖子中蕴含的语义线索。"""
    haystack = f"{title}\n{text}"
    mapping = {
        "proposal": ["建议", "提议", "提案", "方案", "是否", "拟"],
        "practice": ["流程", "规定", "安排", "执行", "报名", "分组", "制度", "规则"],
        "benefit": ["优点", "好处", "有利于", "便于", "提高", "避免", "保证"],
        "risk": ["问题", "风险", "缺点", "不足", "争议", "反对", "事故", "取消"],
        "outcome": ["结果", "总结", "复盘", "最终", "实际", "完成", "公示"],
    }
    return ",".join(k for k, words in mapping.items()
                    if any(w in haystack for w in words))


def source_label(title: str, text: str) -> str:
    """检测是否为执委会/理事会/财务管理类帖子。"""
    haystack = f"{title}\n{text[:800]}"
    patterns = [
        r"第[一二三四五六七八九十百零〇0-9]+次[^，。\n]{0,12}执委会",
        r"[0-9]{4}[^，。\n]{0,12}执委会",
        r"(春季|秋季|暑期|寒假)[^，。\n]{0,8}执委会",
        r"理事会[^，。\n]{0,24}(通知|决议|任命|提醒)",
        r"财务(制度|总结|公开|报销)",
        # 山大车协特有
        r"会长[^，。\n]{0,8}(换届|选举|任命)",
        r"主席团[^，。\n]{0,12}(会议|通知|决议)",
    ]
    for pattern in patterns:
        match = re.search(pattern, haystack)
        if match:
            return match.group(0)
    return ""


# ============================================================================
# 格式转换
# ============================================================================

def convert_record(record: dict, max_chars: int, overlap: int) -> list[dict]:
    """将一条 post 记录转换为一条或多条检索格式记录。

    Args:
        record:  threads.jsonl 的单行 JSON
        max_chars: 分块最大字符数
        overlap:   分块重叠字符数

    Returns:
        [{text, source, id}, ...]  — faiss_meta.jsonl 同构格式
    """
    fid = record.get("fid", 0)
    tid = record.get("tid", 0)
    board = record.get("board", "")
    title = record.get("title", "")
    author = record.get("author", "")
    floor = record.get("floor", 0)
    post_time = record.get("post_time", "")
    url = record.get("url", "")
    content = normalize_text(record.get("content", ""))
    tags = record.get("tags", [])
    year = record.get("year", "")
    season = record.get("season", "")
    event_type = record.get("event_type", "")

    if not content.strip():
        return []

    # 计算 cues 和 source_label
    cue_str = cues(title, content)
    label = source_label(title, content)

    # 拼装 text 前缀
    prefix = (
        f"【{board}】《{title}》第{floor}楼｜{author}｜{post_time}\n"
        f"链接: {url}\n"
    )

    # 如果有标签信息，追加到 text 前缀
    if tags:
        prefix += f"标签: {'，'.join(tags)}\n"
    if cue_str:
        prefix += f"语义线索: {cue_str}\n"

    prefix += "\n"

    # 分块
    chunks = split_text(content, max_chars, overlap)
    results: list[dict] = []
    for chunk_index, chunk in enumerate(chunks, start=1):
        doc = prefix + chunk
        results.append({
            "text": doc,
            "source": {
                "fid": fid,                    # 版块 ID
                "board": board,
                "tid": tid,
                "title": title,
                "floor": floor,
                "chunk_index": chunk_index,
                "chunk_count": len(chunks),
                "author": author,
                "post_time": post_time,
                "url": url,
                "source_label": label,
                "cues": cue_str,
                # 额外标签字段，保留便于后续检索
                "tags": tags,
                "year": year,
                "season": season,
                "event_type": event_type,
            },
            "id": f"fid{fid}_tid{tid}_f{floor}_idx{chunk_index}",
        })

    return results


# ============================================================================
# 主流程
# ============================================================================

def main():
    # 强制 UTF-8 输出 —— 解决 Windows GBK 终端乱码
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

    parser = argparse.ArgumentParser(
        description="threads.jsonl → faiss_meta.jsonl 格式转换"
    )
    parser.add_argument("--input", "-i",
                        default=str(DATA_DIR / "threads.jsonl"),
                        help="输入 JSONL 文件路径")
    parser.add_argument("--output", "-o",
                        default=str(DATA_DIR / "casdu_meta.jsonl"),
                        help="输出 JSONL 文件路径")
    parser.add_argument("--max-chars", type=int, default=1500,
                        help="分块最大字符数（默认 1500）")
    parser.add_argument("--overlap", type=int, default=200,
                        help="分块重叠字符数（默认 200）")
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if not input_path.exists():
        print(f"错误：输入文件不存在: {input_path}")
        print("请先运行 scraper.py --full 爬取数据")
        return

    t0 = time.time()
    total_posts = 0
    total_chunks = 0

    with open(input_path, "r", encoding="utf-8") as fin, \
         open(output_path, "w", encoding="utf-8") as fout:

        for line in fin:
            if not line.strip():
                continue
            record = json.loads(line)
            chunks = convert_record(record, args.max_chars, args.overlap)
            for chunk in chunks:
                fout.write(_safe_dumps(chunk) + "\n")
                total_chunks += 1
            total_posts += 1

            if total_posts % 500 == 0:
                elapsed = time.time() - t0
                print(f"  已处理 {total_posts} 帖 → {total_chunks} chunk "
                      f"({elapsed:.0f}s)")

    elapsed = time.time() - t0
    in_size = input_path.stat().st_size / 1024 / 1024
    out_size = output_path.stat().st_size / 1024 / 1024

    print(f"\n转换完成！")
    print(f"  输入: {input_path} ({in_size:.1f} MB)")
    print(f"  帖子数: {total_posts}")
    print(f"  输出: {output_path} ({out_size:.1f} MB)")
    print(f"  chunk 总数: {total_chunks}")
    print(f"  平均每帖 {total_chunks / max(total_posts, 1):.1f} chunk")
    print(f"  耗时: {elapsed:.0f}s")
    print(f"\n下一步：将 {output_path} 喂给 embedding 模型构建向量索引")


if __name__ == "__main__":
    main()
