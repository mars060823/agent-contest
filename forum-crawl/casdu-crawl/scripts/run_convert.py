#!/usr/bin/env python3
"""
格式转换 — CLI 入口
等价于 python -m casdu_crawl.convert_for_retrieval
"""
import sys
import io
from pathlib import Path

# 强制 UTF-8 输出 —— 解决 Windows GBK 终端乱码
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
if hasattr(sys.stderr, "buffer"):
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from casdu_crawl.convert_for_retrieval import main

if __name__ == "__main__":
    main()
