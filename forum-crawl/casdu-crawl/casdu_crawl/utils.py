#!/usr/bin/env python3
"""
bbs.casdu.cn 爬虫 — 工具函数
HTTP 请求、GBK 容错解码、HTML 清洗、页面解析
"""

import re
import time
import random
import logging
from typing import Optional
from urllib.parse import urljoin

import requests

from casdu_crawl.config import (
    MIN_DELAY, MAX_DELAY, RETRY_DELAY, MAX_RETRIES, TIMEOUT,
    USER_AGENT, BASE_URL, LOGIN_URL,
    CASDU_USER, CASDU_PASS,
)

logger = logging.getLogger("casdu")


# ============================================================================
# HTTP 工具
# ============================================================================

def create_session() -> requests.Session:
    """创建 requests.Session，若环境变量有凭据则自动登录。

    Discuz! X3.2 登录流程:
      POST member.php?mod=logging&action=login&loginsubmit=yes
        username=<CASDU_USER>
        password=<CASDU_PASS>
        cookietime=2592000
      返回 → Set-Cookie: dOIi_2132_auth, dOIi_2132_saltkey
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    })

    if CASDU_USER and CASDU_PASS:
        logger.info("检测到登录凭据，尝试登录...")
        try:
            resp = session.post(
                LOGIN_URL,
                data={
                    "username": CASDU_USER,
                    "password": CASDU_PASS,
                    "cookietime": "2592000",
                    "quickforward": "yes",
                    "handlekey": "ls",
                },
                headers={
                    "Referer": f"{BASE_URL}/forum.php",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=TIMEOUT,
            )
            # Discuz! 登录成功会 302 跳转到 forum.php
            if "auth" in session.cookies.get_dict() or resp.status_code == 302:
                logger.info("登录成功！")
            else:
                # 检查是否登录失败（页面出现"登录失败"）
                if "登录失败" in resp.text or "密码错误" in resp.text:
                    logger.warning("登录失败：用户名或密码错误")
                else:
                    logger.info("已发送登录请求（状态码=%d）", resp.status_code)
            time.sleep(1)  # 登录后稍作等待
        except Exception as e:
            logger.warning("登录请求异常: %s（将以匿名模式继续）", e)
    else:
        logger.info("未设置 CASDU_USER/CASDU_PASS 环境变量，使用匿名模式")

    return session


def fetch(url: str, session: requests.Session, force_no_delay: bool = False) -> str:
    """发起 GET 请求，返回 GBK→UTF-8 解码后的 HTML 文本。

    - 每次调用后自动 sleep(random.uniform(MIN_DELAY, MAX_DELAY))
    - 遇 429/5xx → 退避重试（最多 MAX_RETRIES 次）
    - force_no_delay=True 时不 sleep（用于连续调用同一个版块的翻页）

    Args:
        url: 目标 URL
        session: requests.Session 实例
        force_no_delay: 是否跳过请求后延迟

    Returns:
        解码后的 HTML 文本

    Raises:
        RuntimeError: 重试耗尽后仍失败
    """
    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=TIMEOUT)
        except requests.RequestException as e:
            last_error = e
            logger.warning("请求失败 (attempt %d/%d): %s — %s",
                           attempt, MAX_RETRIES, url, e)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
            continue

        # 退避重试
        if resp.status_code in (429, 503):
            logger.warning("HTTP %d，退避 %ds — %s",
                           resp.status_code, RETRY_DELAY, url)
            time.sleep(RETRY_DELAY)
            continue
        if resp.status_code >= 500:
            logger.warning("HTTP %d (attempt %d/%d) — %s",
                           resp.status_code, attempt, MAX_RETRIES, url)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)
            last_error = RuntimeError(f"HTTP {resp.status_code}")
            continue
        if resp.status_code == 404:
            logger.warning("404 Not Found: %s", url)
            return ""  # 不重试 404
        if resp.status_code >= 400:
            logger.warning("HTTP %d: %s", resp.status_code, url)
            return ""

        # 解码 GBK → UTF-8
        try:
            html = resp.content.decode("gbk", errors="replace")
        except (LookupError, UnicodeDecodeError):
            html = resp.content.decode("gb18030", errors="replace")

        break
    else:
        raise RuntimeError(
            f"重试 {MAX_RETRIES} 次后仍失败: {url} — "
            f"最后错误: {last_error}"
        )

    # 速率限制（调用方可通过 force_no_delay 跳过）
    if not force_no_delay:
        delay = random.uniform(MIN_DELAY, MAX_DELAY)
        time.sleep(delay)

    return html


# ============================================================================
# 版块发现
# ============================================================================

def discover_boards(session: requests.Session) -> list[tuple[int, str]]:
    """从 forum.php 首页动态抓取完整版块列表。

    Discuz! 首页的版块链接格式（可能有 &amp; 或 & 两种写法）：
      <a href="forum.php?mod=forumdisplay&fid=XXX">版块名</a>
      <a href="forum.php?mod=forumdisplay&amp;fid=XXX">版块名</a>

    Returns:
        [(fid, name), ...] 按 fid 排序
    """
    html = fetch(f"{BASE_URL}/forum.php", session, force_no_delay=True)

    # 匹配 forumdisplay 链接（兼容 &amp; 和 &）
    pattern = re.compile(
        r'href="forum\.php\?mod=forumdisplay(?:&amp;|&)fid=(\d+)"[^>]*>'
        r'(.*?)'
        r'</a>',
        re.DOTALL,
    )
    matches = pattern.findall(html)

    seen: set[int] = set()
    boards: list[tuple[int, str]] = []
    for fid_str, raw_title in matches:
        fid = int(fid_str)

        # 清洗标题：先去标签、再解码实体、再去空白
        title = re.sub(r'<[^>]+>', '', raw_title).strip()
        title = re.sub(r'&nbsp;', ' ', title)
        title = re.sub(r'\s+', '', title)

        if not title:
            continue  # 跳过图标链接（只有 <img> 无文字）

        if fid in seen:
            continue
        seen.add(fid)
        boards.append((fid, title))

    boards.sort(key=lambda x: x[0])
    logger.info("发现 %d 个版块", len(boards))
    return boards


# ============================================================================
# 版块页解析（archiver）
# ============================================================================

def parse_board_page(html: str) -> list[tuple[int, str]]:
    """从 archiver 版块页提取线程列表。

    Archiver 版块页格式：
      <li><a href="?tid-XXXX.html">帖子标题</a></li>

    Args:
        html: archiver 版块页 HTML

    Returns:
        [(tid, title), ...]
    """
    # Archiver 的线程链接格式
    pattern = re.compile(
        r'<li>\s*<a\s+href="\?tid-(\d+)\.html"[^>]*>'
        r'(.*?)'
        r'</a>',
        re.DOTALL,
    )
    matches = pattern.findall(html)

    threads: list[tuple[int, str]] = []
    for tid_str, raw_title in matches:
        tid = int(tid_str)
        # 清洗标题
        title = re.sub(r'<[^>]+>', '', raw_title).strip()
        title = re.sub(r'&nbsp;', ' ', title)
        title = re.sub(r'\s+', ' ', title)
        if title:
            threads.append((tid, title))

    return threads


def parse_board_max_page(html: str) -> int:
    """从 archiver 版块页提取最大页码。

    Archiver 分页格式：
      <a href="?fid-X.html&page=35">35</a>

    Returns:
        最大页码（至少为 1）
    """
    # 匹配所有页码
    page_links = re.findall(
        r'href="\?fid-\d+\.html&page=(\d+)"',
        html,
    )
    if not page_links:
        return 1
    return max(int(p) for p in page_links)


# ============================================================================
# 帖子详情页解析（archiver）
# ============================================================================

def parse_thread_posts(html: str) -> list[dict]:
    """从 archiver 帖子页提取所有回帖。

    Archiver 帖子页格式：
      <div class="author">
        <strong>作者名</strong>
        发表于 YYYY-M-D HH:MM:SS
      </div>
      <h3>帖子标题（仅第一帖）</h3>
      正文内容...
      (然后下一个 <div class="author"> 或页导航)

    Args:
        html: archiver 帖子页 HTML

    Returns:
        [{author, post_time, content}, ...] 按楼层顺序
    """
    # 以 <p class="author"> 或 <div class="author"> 为边界切分
    # archiver 使用 <p class="author">（Discuz! X3.2 标准）
    # 注意：class 可能在引号内
    parts = re.split(r'<\w+\s+class="author">', html, flags=re.IGNORECASE)

    if len(parts) < 2:
        return []

    posts: list[dict] = []
    for part in parts[1:]:  # 跳过 header
        # 提取 <strong> 内的作者
        author_m = re.search(r'<strong>(.*?)</strong>', part, re.DOTALL)
        author = author_m.group(1).strip() if author_m else "未知"

        # 提取 "发表于" 后的时间
        time_m = re.search(r'发表于\s*([^<]+)', part)
        post_time = time_m.group(1).strip() if time_m else ""

        # 正文：</p>（关闭 author 段落）之后的内容
        # Archiver 中 author 标签可能是 <p class="author"> 或 <div class="author">
        # 关闭标签可能是 </p> 或 </div>
        close_tag = "</p>" if "</p>" in part[:200] else "</div>"
        tag_end = part.find(close_tag)
        if tag_end < 0:
            content = part
        else:
            content = part[tag_end + len(close_tag):]

        # 去掉 <h3>...</h3> 中的帖子标题（仅首帖可能有）
        content = re.sub(r'<h3>.*?</h3>', '', content, flags=re.DOTALL | re.IGNORECASE)

        # 清洗 HTML 标签
        content = clean_content(content)
        content = content.strip()

        if content or author != "未知":
            posts.append({
                "author": author,
                "post_time": post_time,
                "content": content,
            })

    return posts


def parse_thread_max_page(html: str) -> int:
    """从 archiver 帖子页提取最大页码。

    Archiver 帖子分页格式：
      <a href="?tid-X.html&page=3">3</a>

    Returns:
        最大页码（至少为 1）
    """
    # 匹配 tid-X.html&page=N 格式
    page_links = re.findall(
        r'href="\?tid-\d+\.html&page=(\d+)"',
        html,
    )
    if not page_links:
        return 1
    return max(int(p) for p in page_links)


# ============================================================================
# 文本清洗
# ============================================================================

def clean_content(raw: str) -> str:
    """清洗 HTML 标签，保留纯文本段落。

    参考 chexie-knowledge build_chexie_faiss.py 的 normalize_text()：

    - \r\n, \r → \n（统一换行）
    - 　（全角空格）→ 半角空格
    - �（替换字符）→ 删除
    - 压缩连续空白
    - 保留自然段落结构

    Args:
        raw: 原始 HTML 文本

    Returns:
        清洗后的纯文本
    """
    text = raw

    # 1. 替换常见 HTML 标签为换行或空格
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<p[^>]*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</?div[^>]*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<hr[^>]*>', '\n', text, flags=re.IGNORECASE)

    # 2. 去除所有 HTML 标签
    text = re.sub(r'<[^>]+>', '', text)

    # 3. 解码 HTML 实体
    text = html_unescape(text)

    # 4. 统一换行
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    # 5. 全角空格 → 半角空格
    text = text.replace('　', ' ')

    # 6. 删除替换字符
    text = text.replace('�', '')

    # 7. 压缩连续空白（但不合并换行）
    text = re.sub(r'[ \t]+', ' ', text)

    # 8. 压缩多行空行
    text = re.sub(r'\n{3,}', '\n\n', text)

    # 9. 去除首尾空白
    text = text.strip()

    return text


def html_unescape(text: str) -> str:
    """解码常见 HTML 实体。"""
    entities = {
        "&nbsp;": " ",
        "&amp;": "&",
        "&lt;": "<",
        "&gt;": ">",
        "&quot;": '"',
        "&#39;": "'",
        "&apos;": "'",
        "&mdash;": "—",
        "&ndash;": "–",
        "&hellip;": "…",
        "&lsquo;": "'",
        "&rsquo;": "'",
        "&ldquo;": '"',
        "&rdquo;": '"',
    }
    for entity, char in entities.items():
        text = text.replace(entity, char)
    # 十进制数字实体 &#XXXXX;
    text = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1)))
                  if int(m.group(1)) < 0x110000 else '', text)
    # 十六进制数字实体 &#xXXXX;
    text = re.sub(r'&#x([0-9a-fA-F]+);', lambda m: chr(int(m.group(1), 16))
                  if int(m.group(1), 16) < 0x110000 else '', text)
    return text


# ============================================================================
# 主站 forum.php 帖子元数据解析（评分/支持/反对/收藏/点评）
# ============================================================================

def parse_thread_meta(html: str) -> list[dict]:
    """从主站 forum.php?mod=viewthread 页面提取每个楼层的互动元数据。

    仅提取公开的论坛互动数据，不采集头像/邮箱/手机号等隐私信息。

    Args:
        html: 主站帖子页 HTML（GBK 已解码为 Unicode）

    Returns:
        [{pid, rating_count, rating_coins, rating_details,
          recommend_add, recommend_subtract, favorite_count,
          comment_count, comments}, ...]
    """
    results: list[dict] = []

    # 按楼层切分（div[id^="post_"] 或 table[id^="pid"]）
    post_blocks = re.findall(
        r'<(?:div|table)\s+id="(?:post_|pid)(\d+)"[^>]*>(.*?)'
        r'(?=<(?:div|table)\s+id="(?:post_|pid)\d+"|$)',
        html, re.DOTALL,
    )

    for pid_str, block in post_blocks:
        pid = int(pid_str)
        meta: dict = {"pid": pid}

        # --- 评分 ---
        rater_m = re.search(
            r'参与人数[^<]*<span[^>]*class="[^"]*xi1[^"]*"[^>]*>(\d+)',
            block,
        )
        meta["rating_count"] = int(rater_m.group(1)) if rater_m else 0

        coin_m = re.search(
            r'金币[^<]*<i><span[^>]*class="[^"]*xi1[^"]*"[^>]*>\s*\+?(\d+)',
            block,
        )
        meta["rating_coins"] = int(coin_m.group(1)) if coin_m else 0

        # 每条评分：{uid, username, coins, reason}
        rate_rows = re.findall(
            r'<tr\s+id="rate_\d+_(\d+)"[^>]*>'
            r'.*?<a[^>]*home\.php\?mod=space[^>]*>([^<]+)</a>'
            r'.*?<td[^>]*class="[^"]*xi1[^"]*"[^>]*>\s*\+?\s*(\d+)'
            r'.*?<td[^>]*class="[^"]*xg1[^"]*"[^>]*>([^<]*)',
            block, re.DOTALL,
        )
        meta["rating_details"] = [
            {"uid": int(uid), "username": name.strip(),
             "coins": int(coins), "reason": reason.strip()}
            for uid, name, coins, reason in rate_rows
        ]

        # --- 支持/反对（打气/爆胎） ---
        support_m = re.search(r'id="recommendv_add[^"]*"[^>]*>(\d+)', block)
        meta["recommend_add"] = int(support_m.group(1)) if support_m else 0

        oppose_m = re.search(r'id="recommendv_subtract[^"]*"[^>]*>(\d+)', block)
        meta["recommend_subtract"] = int(oppose_m.group(1)) if oppose_m else 0

        # --- 收藏 ---
        fav_m = re.search(r'id="favoritenumber[^"]*"[^>]*>(\d+)', block)
        meta["favorite_count"] = int(fav_m.group(1)) if fav_m else 0

        # --- 点评（楼中楼短评） ---
        comment_block = re.search(
            r'<div[^>]*class="[^"]*pstl[^"]*"[^>]*>(.*?)</div>\s*</div>\s*</div>',
            block, re.DOTALL,
        )
        comments: list[dict] = []
        if comment_block:
            # 每条点评：用户链接 + 点评内容
            cmt_items = re.findall(
                r'home\.php\?mod=space(?:&amp;|&)uid=(\d+)[^>]*>'
                r'.*?<a[^>]*>([^<]+)</a>',
                comment_block.group(1), re.DOTALL,
            )
            # 点评正文在用户名链接之后
            cmt_texts = re.findall(
                r'</a>\s*([^<]{1,200})',
                comment_block.group(1),
            )
            for i, (uid, name) in enumerate(cmt_items):
                text = cmt_texts[i].strip() if i < len(cmt_texts) else ""
                comments.append({"uid": int(uid), "username": name.strip(), "content": text})

        meta["comments"] = comments
        meta["comment_count"] = len(comments)

        results.append(meta)

    return results


def fetch_thread_meta(tid: int, page: int, session) -> list[dict]:
    """获取指定帖子指定页的所有楼层互动元数据。

    请求主站 forum.php?mod=viewthread&tid={tid}&page={page}，
    仅提取互动数据（评分/支持/反对/收藏/点评），不含头像等隐私信息。
    """
    url = f"{BASE_URL}/forum.php?mod=viewthread&tid={tid}&page={page}"
    html = fetch(url, session, force_no_delay=True)
    return parse_thread_meta(html)


# ============================================================================
# 便利函数
# ============================================================================

def make_board_url(fid: int, page: int = 1) -> str:
    """生成 archiver 版块页 URL。"""
    if page <= 1:
        return f"{BASE_URL}/archiver/?fid-{fid}.html"
    return f"{BASE_URL}/archiver/?fid-{fid}.html&page={page}"


def make_thread_url(tid: int, page: int = 1) -> str:
    """生成 archiver 帖子页 URL。"""
    if page <= 1:
        return f"{BASE_URL}/archiver/?tid-{tid}.html"
    return f"{BASE_URL}/archiver/?tid-{tid}.html&page={page}"


def thread_web_url(tid: int) -> str:
    """生成主站帖子链接（用于引用）。"""
    return f"{BASE_URL}/forum.php?mod=viewthread&tid={tid}"


def format_duration(seconds: float) -> str:
    """格式化时长为可读字符串。"""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        return f"{seconds / 60:.1f}m"
    else:
        h = seconds // 3600
        m = (seconds % 3600) / 60
        return f"{h:.0f}h{m:.0f}m"
