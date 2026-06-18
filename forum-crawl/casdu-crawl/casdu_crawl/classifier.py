#!/usr/bin/env python3
"""
bbs.casdu.cn 爬虫 — 自动分类与标签引擎

参考 chexie-knowledge build_entities.py 的字典匹配 + 正则提取模式。
从帖子标题 + 正文中提取 7 类标签：

  1. year        — 年份（如 2026）
  2. season      — 学期（如 春、秋、暑、寒、冬）
  3. event_type  — 事件类型（如 通知、总结、报名）
  4. activity_type — 活动类型（如 拉练、远征、体训）
  5. routes      — 路线名（如 黄巢、玉符河）
  6. roles       — 角色/职务（如 押后、队医）
  7. problems    — 问题类型（如 扎胎、摔车）

输出格式：
  {
    "tags": ["拉练", "黄巢", "2026春", ...],
    "year": "2026",
    "season": "春",
    "event_type": "总结",
    "activity_type": "拉练",
    "routes": ["黄巢"],
    "roles": ["押后", "队医"],
    "problems": [],
  }
"""

import re
from typing import Optional

from casdu_crawl.config import (
    ROUTES, PROBLEMS, ROLES, EVENT_TYPES, ACTIVITY_TYPES,
    YEAR_PATTERN, SEASON_PATTERN,
)


def classify(title: str, content: str = "", board_name: str = "") -> dict:
    """对一条帖子进行自动分类和标签化。

    Args:
        title:   帖子标题
        content: 帖子正文
        board_name: 所在版块名（辅助分类）

    Returns:
        标签字典
    """
    # 合并标题 + 正文前 500 字作为快速扫描区域
    scan_text = f"{title}\n{content[:500]}"

    # 1. 年份 + 学期
    year = _extract_year(scan_text, title)
    season = _extract_season(scan_text, title)

    # 2. 事件类型（从标题提取）
    event_type = _extract_event_type(title)

    # 3. 活动类型
    activity_type = _extract_activity_type(title, scan_text)

    # 4. 路线
    routes = _extract_routes(scan_text)

    # 5. 角色
    roles = _extract_roles(content)

    # 6. 问题
    problems = _extract_problems(content)

    # 7. 合并生成 tags
    tags = _build_tags(
        year=year,
        season=season,
        event_type=event_type,
        activity_type=activity_type,
        routes=routes,
        roles=roles,
        problems=problems,
        board_name=board_name,
    )

    return {
        "tags": tags,
        "year": year,
        "season": season,
        "event_type": event_type,
        "activity_type": activity_type,
        "routes": routes,
        "roles": roles,
        "problems": problems,
    }


# ============================================================================
# 各维度提取函数
# ============================================================================

def _extract_year(text: str, title: str) -> str:
    """从标题和正文中提取年份。

    优先从标题中提取（更准确），fallback 为正文。
    支持的格式：
      - 2026、2025、2018...
      - 26春、25秋...（两位年份+学期）
    """
    # 标题优先
    title_years = YEAR_PATTERN.findall(title)
    if title_years:
        return title_years[0]

    # 正文 fallback
    text_years = YEAR_PATTERN.findall(text)
    if text_years:
        return text_years[0]

    # 尝试两位年份 + 学期（如 26春 → 2026）
    m = re.search(r'^(\d{2})\s*[春秋暑寒冬]', title)
    if m:
        yy = int(m.group(1))
        return str(2000 + yy if yy < 50 else 1900 + yy)

    m = re.search(r'(\d{2})\s*[春秋暑寒冬]', title)
    if m:
        yy = int(m.group(1))
        return str(2000 + yy if yy <= 50 else 1900 + yy)

    return ""


def _extract_season(text: str, title: str) -> str:
    """提取学期（春/秋/暑/寒/冬）。

    如：26春 → 春，2025秋 → 秋
    """
    # 标题优先
    m = SEASON_PATTERN.search(title)
    if m:
        return m.group(2)

    m = SEASON_PATTERN.search(text)
    if m:
        return m.group(2)

    # 独立学期词
    for s in ["暑", "寒", "冬"]:
        if s in title[:30]:
            return s
    if "春" in title[:30]:
        return "春"
    if "秋" in title[:30]:
        return "秋"

    return ""


def _extract_event_type(title: str) -> str:
    """从标题提取事件类型（通知/总结/报名/探路/选拔/训练/比赛/讨论/复盘/公告）。

    匹配优先级：精确词 > 模糊词，标题前半段权重更高。
    """
    title_prefix = title[:40]  # 标题前 40 字权重更高

    for et in EVENT_TYPES:
        if et in title_prefix:
            return et
    for et in EVENT_TYPES:
        if et in title:
            return et

    return ""


def _extract_activity_type(title: str, scan_text: str) -> str:
    """提取活动类型（拉练/远征/体训/行疆/冬游/实践/支教/比赛）。

    从标题中匹配，优先完整词。
    """
    for at in ACTIVITY_TYPES:
        if at in title:
            return at

    for at in ACTIVITY_TYPES:
        if at in scan_text[:200]:
            return at

    return ""


def _extract_routes(text: str) -> list[str]:
    """从正文中匹配已知路线名。

    去重，按首次出现顺序排列。
    """
    found: list[str] = []
    for route in ROUTES:
        if route in text:
            found.append(route)
    return found


def _extract_roles(text: str) -> list[str]:
    """从正文中匹配已知角色/职务。

    在全文搜索（角色通常出现在正文中，非标题）。
    """
    found: list[str] = []
    for role in ROLES:
        if role in text:
            found.append(role)
    return found


def _extract_problems(text: str) -> list[str]:
    """从正文中匹配已知问题类型。

    问题通常在描述状况时出现。
    """
    found: list[str] = []
    for problem in PROBLEMS:
        if problem in text:
            found.append(problem)
    return found


# ============================================================================
# 标签合并
# ============================================================================

def _build_tags(
    year: str,
    season: str,
    event_type: str,
    activity_type: str,
    routes: list[str],
    roles: list[str],
    problems: list[str],
    board_name: str,
) -> list[str]:
    """合并所有维度的标签，去重排序。"""
    tags: list[str] = []

    # 年份标签
    if year:
        tags.append(year)

    # 学期标签
    if season:
        if year and len(year) == 4:
            yy = year[2:]
            tags.append(f"{yy}{season}")
        else:
            tags.append(season)

    # 事件类型
    if event_type:
        tags.append(event_type)

    # 活动类型
    if activity_type:
        tags.append(activity_type)

    # 路线（每个都是一条标签）
    tags.extend(routes)

    # 角色
    tags.extend(roles)

    # 问题
    tags.extend(problems)

    # 版块名中包含的关键词
    # 如 "远征·2026" → 标签 "远征"
    if "·" in board_name:
        parts = board_name.split("·")
        tags.append(parts[0])

    # 去重并保持顺序
    seen: set[str] = set()
    unique: list[str] = []
    for t in tags:
        if t and t not in seen:
            seen.add(t)
            unique.append(t)

    return unique
