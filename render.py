"""把 parser 的结构化结果排版成 TXT（纯字符串函数）。

输出规范见方案 §11：文件头 80 个 ``=``、楼层分隔 5 个 ``=``、缺失占位统一为
``[缺失: 字段名]``、块与块之间恰好一个空行、文件末尾一个换行。

纪律：不读写文件、不导入网络库、不打印日志；``render_txt`` 供单测/小规模调用，
engine 流式写出时使用 ``render_header`` + 逐条 ``render_post``（规则与
``render_txt`` 一致：块间 ``"\\n\\n"``，文件末尾 ``"\\n"``）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Iterable, List, Optional, Sequence

if TYPE_CHECKING:  # 仅类型标注用；运行时不依赖解析模块，保持模块为纯字符串处理。
    from nga_parser import ParsedPost

#: 文件头分隔线（80 个 ``=``）。
HEADER_SEPARATOR = "=" * 80
#: 楼层分隔线前缀（5 个 ``=``）。
POST_SEPARATOR = "=" * 5

PLACEHOLDER_LOU = "[缺失: 楼层]"
PLACEHOLDER_AUTHOR = "[缺失: 作者]"
PLACEHOLDER_TIME = "[缺失: 时间]"
PLACEHOLDER_BODY = "[缺失: 正文]"
PLACEHOLDER_TITLE = "[缺失: 标题]"
PLACEHOLDER_OP_AUTHOR = "[缺失: 楼主]"
PLACEHOLDER_TOTAL_PAGES = "[缺失: 总页数]"
PLACEHOLDER_FETCHED = "[缺失: 抓取时间]"
PLACEHOLDER_VERSION = "[缺失: 版本]"

_UTC8 = timezone(timedelta(hours=8))
_FETCHED_FORMAT = "%Y-%m-%d %H:%M:%S"
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


@dataclass
class OutputMeta:
    """TXT 文件头所需的元信息（由 engine 汇总）。"""

    tid: int
    title: Optional[str]
    op_author: Optional[str]
    total_pages: Optional[int]
    page_range: str
    fetched_min: Optional[str]
    fetched_max: Optional[str]
    parser_version: str
    fetcher_version: Optional[str]
    post_count: int
    missing_count: int


def _text_or(value: Optional[str], placeholder: str) -> str:
    """非空字符串去空白，否则返回占位符。"""
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return placeholder


def _format_fetched(value: Optional[str]) -> Optional[str]:
    """把 ISO8601 抓取时间转成固定 UTC+8 的 ``YYYY-MM-DD HH:MM:SS``。

    engine 传入的已是本地格式化字符串（无时区信息）时原样保留；这样 render
    不依赖系统时区，测试也是确定性的。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        moment = datetime.fromisoformat(candidate)
    except ValueError:
        return text
    if moment.tzinfo is None:
        return text
    return moment.astimezone(_UTC8).strftime(_FETCHED_FORMAT)


def _fetched_text(meta: OutputMeta) -> str:
    """``min ~ max``；两端都缺才用占位符。"""
    earliest = _format_fetched(meta.fetched_min)
    latest = _format_fetched(meta.fetched_max)
    if earliest is None and latest is None:
        return PLACEHOLDER_FETCHED
    if earliest is None:
        return latest or PLACEHOLDER_FETCHED
    if latest is None:
        return earliest
    return f"{earliest} ~ {latest}"


def _format_total_pages(total_pages: Optional[int]) -> str:
    if isinstance(total_pages, int) and not isinstance(total_pages, bool):
        return str(total_pages)
    return PLACEHOLDER_TOTAL_PAGES


def render_header(meta: OutputMeta) -> str:
    """渲染文件头；以 80 个 ``=`` 行开头/结尾，末尾不带换行。"""
    title = _text_or(meta.title, PLACEHOLDER_TITLE)
    op_author = _text_or(meta.op_author, PLACEHOLDER_OP_AUTHOR)
    total_pages = _format_total_pages(meta.total_pages)
    page_range = _text_or(meta.page_range, "-")
    parser_version = _text_or(meta.parser_version, PLACEHOLDER_VERSION)
    fetcher_version = _text_or(meta.fetcher_version, PLACEHOLDER_VERSION)

    lines = [
        HEADER_SEPARATOR,
        "NGA 帖子导出",
        f"TID: {meta.tid}",
        f"标题: {title}",
        f"楼主: {op_author}",
        f"总页数: {total_pages}    本次收录页: {page_range}",
        f"抓取时间: {_fetched_text(meta)}",
        f"parser_version: {parser_version}    fetcher_version: {fetcher_version}",
        f"收录楼层: {meta.post_count}    字段缺失: {meta.missing_count} 处",
        HEADER_SEPARATOR,
    ]
    return "\n".join(lines)


def _normalize_body(raw: str) -> str:
    """正文归一行级空白：去行尾空白、连续 3+ 换行压为 2、去首尾空行。"""
    value = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in value.split("\n")]
    value = "\n".join(lines)
    value = _MULTI_NEWLINE_RE.sub("\n\n", value)
    return value.strip("\n")


def _quote_lines(inner: str) -> List[str]:
    """引用块每行加 ``> ``；空行只留 ``>``（避免行尾空白）。"""
    lines: List[str] = []
    for line in inner.split("\n"):
        lines.append("> " + line if line.strip() else ">")
    return lines


def _render_blocks(blocks: Iterable["object"]) -> str:
    """把 block 序列排成正文文本；引用/图片为块级，文本/链接为内联。"""
    parts: List[str] = []
    for block in blocks:
        kind = getattr(block, "kind", "")
        text = getattr(block, "text", "") or ""
        children = getattr(block, "children", None) or []
        if kind == "image":
            url = str(text).strip()
            if url:
                parts.append("\n[图片: " + url + "]\n")
        elif kind == "quote":
            inner = _render_blocks(children)
            if inner:
                parts.append("\n" + "\n".join(_quote_lines(inner)) + "\n")
        elif kind == "link":
            parts.append(str(text))
        else:  # text 或未知类型：按普通文本处理
            parts.append(str(text))
    return _normalize_body("".join(parts))


def _format_author(post: "ParsedPost") -> Optional[str]:
    author = getattr(post, "author", None)
    if isinstance(author, str):
        text = author.strip()
        if text:
            return text
    return None


def _format_lou(post: "ParsedPost") -> str:
    lou = getattr(post, "lou", None)
    if isinstance(lou, int) and not isinstance(lou, bool):
        return str(lou)
    return PLACEHOLDER_LOU


def _format_posted_at(post: "ParsedPost") -> str:
    posted_at = getattr(post, "posted_at", None)
    if isinstance(posted_at, str):
        text = posted_at.strip()
        if text:
            return text
    return PLACEHOLDER_TIME


def render_post(post: "ParsedPost") -> str:
    """渲染单个楼层块；末尾不带换行。"""
    lou = _format_lou(post)
    author = _format_author(post)
    author_text = author or PLACEHOLDER_AUTHOR
    time_text = _format_posted_at(post)

    lines: List[str] = [
        f"{POST_SEPARATOR} 第 {lou} 楼 | {author_text} | {time_text} {POST_SEPARATOR}",
    ]
    author_line = f"作者: {author_text}"
    if getattr(post, "is_op", False) and author is not None:
        author_line += "（楼主）"
    lines.append(author_line)
    lines.append(f"时间: {time_text}")
    lines.append("")
    lines.append("正文:")
    body = _render_blocks(getattr(post, "blocks", None) or [])
    lines.append(body or PLACEHOLDER_BODY)

    edited = getattr(post, "edited", None)
    if isinstance(edited, str) and edited.strip():
        lines.append("")
        lines.append(f"[编辑] {edited.strip()}")
    return "\n".join(lines)


def render_txt(items: Sequence["ParsedPost"], meta: OutputMeta) -> str:
    """串起文件头与所有楼层：块间恰好一个空行，文件末尾恰好一个换行。"""
    parts: List[str] = [render_header(meta)]
    for post in items:
        parts.append(render_post(post))
    return "\n\n".join(parts) + "\n"


__all__ = [
    "OutputMeta",
    "render_header",
    "render_post",
    "render_txt",
    "HEADER_SEPARATOR",
    "POST_SEPARATOR",
    "PLACEHOLDER_LOU",
    "PLACEHOLDER_AUTHOR",
    "PLACEHOLDER_TIME",
    "PLACEHOLDER_BODY",
    "PLACEHOLDER_TITLE",
    "PLACEHOLDER_OP_AUTHOR",
    "PLACEHOLDER_TOTAL_PAGES",
    "PLACEHOLDER_FETCHED",
    "PLACEHOLDER_VERSION",
]
