"""
bbs.casdu.cn 论坛爬虫 — 山大车协论坛全量数据归档工具。

核心模块：
    config    — 配置（版块定义、标签词表、速率控制）
    scraper   — 主控脚本（全量/增量爬取）
    utils     — HTTP 工具、GBK 解码、页面解析
    storage   — 数据持久化（JSONL + SQLite + 检查点）
    classifier — 自动标签分类引擎
    convert_for_retrieval — casdu → chexie-knowledge 格式转换

用法：
    python -m casdu_crawl.scraper --full
    python scripts/run_scraper.py --full
"""

__version__ = "1.0.0"
