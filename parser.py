"""把 crawler 落盘的缓存 dict 解析成结构化数据（纯函数模块）。

输入是 ``cache/{tid}/page_{page:03d}.json`` 的内容（外层 schema 见方案 §7.2，
NGA JSON 原样放在 ``content.data``，见 §7.3）。本模块的纪律：

- 只依赖标准库 ``re`` / ``html`` / ``typing`` / ``dataclasses`` / ``datetime``；
- 不导入网络库、不读写文件、不打日志；所有问题写进 ``ParsedPage.warnings``；
- ``parse_cache`` 对坏数据只降级、不抛业务异常，保证单页坏数据不中断整体解析。

对外接口（engine 依赖，勿改名）：``Block`` / ``ParsedPost`` / ``ParsedPage`` /
``parse_cache`` / ``bbcode_to_blocks``。
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

#: 引用嵌套深度上限；超过后降级为普通文本。
MAX_QUOTE_DEPTH = 20
#: 单页告警条数上限，避免异常页面产生海量日志。
MAX_WARNINGS = 100

#: 帖子级缺失字段名（固定，见方案 §11.5）。
MISSING_LOU = "楼层"
MISSING_AUTHOR = "作者"
MISSING_TIME = "时间"
MISSING_BODY = "正文"

_POST_TIME_FORMAT = "%Y-%m-%d %H:%M"
_UTC8 = timezone(timedelta(hours=8))

#: 毫秒时间戳阈值：超过 1e11 的秒级时间戳约为公元 5138 年，故视为毫秒。
_MILLISECOND_THRESHOLD = 100_000_000_000

_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")
_TRAILING_WS_RE = re.compile(r"[ \t\u00a0\u3000]+$")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_WHITESPACE_RE = re.compile(r"\s+")
_EMOTE_RE = re.compile(r"\[s:[^\[\]]*\]", re.IGNORECASE)
_EDITED_RE = re.compile(r"本帖最后由[^\n]{0,200}?于[^\n]{0,200}?编辑")
_ANY_TAG_RE = re.compile(r"\[/?[A-Za-z][^\[\]]{0,300}\]")
_HTML_TAG_RE = re.compile(r"<[^>]{1,200}>")
_HTML_BREAK_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)

#: 标签：``[*]`` 单列，其余为 ``[name attrs]`` 或 ``[/name]``；中文方括号不会命中。
_TAG_RE = re.compile(
    r"\[(?P<star>\*)\]"
    r"|\[(?P<close>/?)(?P<name>[A-Za-z][A-Za-z0-9:_.\-]*)(?P<attrs>[^\[\]]*)\]"
)

#: 标题里需要去掉的“已知 BBCode 标签”；未知 ASCII 标签保留，避免误删内容。
_TITLE_TAG_NAMES = (
    "b|i|u|del|color|size|font|align|collapse|quote|code|list|table|tr|td|"
    "url|img|pid|uid|center|left|right|indent|hr|br|flash|audio|video|album|"
    "attach|tid|randomblock|lessernuke|item|mark"
)
_TITLE_TAG_RE = re.compile(
    r"\[/?(?:" + _TITLE_TAG_NAMES + r")(?:=[^\[\]]*)?\]", re.IGNORECASE
)


@dataclass
class Block:
    """正文中的一个排版单元。

    - ``kind == "text"``：普通文本，可含换行；
    - ``kind == "quote"``：引用块，``children`` 为内部 block（嵌套引用递归）；
    - ``kind == "image"``：图片，``text`` 为绝对 URL；
    - ``kind == "link"``：链接，``text`` 为已完成排版的内联文本（``TEXT (URL)``）。
    """

    kind: str
    text: str = ""
    children: List["Block"] = field(default_factory=list)


@dataclass
class ParsedPost:
    """单个楼层。"""

    lou: Optional[int]
    pid: Optional[int]
    author: Optional[str]
    author_id: Optional[int]
    posted_at: Optional[str]
    blocks: List[Block]
    edited: Optional[str]
    is_op: bool
    missing_fields: List[str]


@dataclass
class ParsedPage:
    """单页解析结果；``skipped`` 为真时 ``posts`` 为空。"""

    tid: int
    page: int
    page_type: str
    status: str
    title: Optional[str]
    op_author: Optional[str]
    total_pages: Optional[int]
    fetcher_version: Optional[str]
    req_id: Optional[str]
    fetched_at: Optional[str]
    posts: List[ParsedPost]
    warnings: List[str]
    missing_fields: Dict[str, int]
    skipped: bool
    skip_reason: Optional[str]


# ---------------------------------------------------------------------------
# 基础类型转换（全部不抛异常）
# ---------------------------------------------------------------------------


def _as_int(value: Any) -> Optional[int]:
    """宽松转 int；失败返回 ``None``（``bool`` 不视为数字）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            pass
        try:
            number = float(text)
        except ValueError:
            return None
        return int(number) if number.is_integer() else None
    return None


def _as_clean_str(value: Any) -> Optional[str]:
    """非空字符串去空白；其它类型返回 ``None``。"""
    if not isinstance(value, str):
        return None
    text = _ZERO_WIDTH_RE.sub("", value).strip()
    return text or None


def _warn(warnings: List[str], message: str) -> None:
    """追加告警并限制总数，避免异常页面刷爆日志。"""
    if len(warnings) < MAX_WARNINGS:
        warnings.append(message)
    elif len(warnings) == MAX_WARNINGS:
        warnings.append("告警过多，后续同类告警已省略")


# ---------------------------------------------------------------------------
# 标题清洗
# ---------------------------------------------------------------------------


def _clean_title(raw: Any) -> Optional[str]:
    """清洗 ``__T.subject``：去已知 BBCode/HTML 标签并归一空白。

    只去掉已知标签名，因此 ``[写作]``、``[短篇]`` 这类非 BBCode 的方括号文本
    （文学区标题的常见组成部分）会被完整保留。
    """
    if not isinstance(raw, str):
        return None
    value = raw.replace("\r\n", "\n").replace("\r", "\n")
    value = _HTML_BREAK_RE.sub(" ", value)
    value = _HTML_TAG_RE.sub(" ", value)
    value = _EMOTE_RE.sub(" ", value)
    value = _TITLE_TAG_RE.sub("", value)
    value = html.unescape(value)
    value = _ZERO_WIDTH_RE.sub("", value)
    value = _WHITESPACE_RE.sub(" ", value).strip()
    return value or None


# ---------------------------------------------------------------------------
# URL 绝对化（方案 §6.10）
# ---------------------------------------------------------------------------


def absolutize_url(raw: Any) -> str:
    """把 NGA 正文里的图片/链接补全为绝对 URL；无法识别时原样返回。"""
    if not isinstance(raw, str):
        return ""
    url = _ZERO_WIDTH_RE.sub("", html.unescape(raw)).strip().strip("\"'")
    if not url:
        return ""
    lowered = url.lower()
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("./"):
        url = url[2:]
    if url.startswith("/"):
        return "https://bbs.nga.cn" + url
    if url.startswith("mon_"):
        return "https://img.nga.cn/attachments/" + url
    if url.startswith("attachments/"):
        return "https://img.nga.cn/" + url
    if url.startswith("read.php"):
        return "https://bbs.nga.cn/" + url
    return url


# ---------------------------------------------------------------------------
# BBCode → Block
# ---------------------------------------------------------------------------


def _clean_text_value(raw: str) -> str:
    """文本块归一：换行统一、去行尾空白、压缩连续空行；保留段落边界。"""
    value = raw.replace("\r\n", "\n").replace("\r", "\n")
    value = _ZERO_WIDTH_RE.sub("", value)
    value = html.unescape(value)
    lines = [_TRAILING_WS_RE.sub("", line) for line in value.split("\n")]
    value = "\n".join(lines)
    return _MULTI_NEWLINE_RE.sub("\n\n", value)


def _plain_inline(raw: str) -> str:
    """把内联 BBCode 压成单行纯文本（用于链接文字、pid/uid 内容）。"""
    value = _EMOTE_RE.sub("[表情]", raw)
    value = _ANY_TAG_RE.sub("", value)
    value = html.unescape(value)
    value = _ZERO_WIDTH_RE.sub("", value)
    return _WHITESPACE_RE.sub(" ", value).strip()


def _strip_all_tags(raw: str) -> str:
    """去掉所有 ASCII 标签但保留文字（保留表情占位）；交最终文本清洗再 unescape。"""
    value = _EMOTE_RE.sub("[表情]", raw)
    return _ZERO_WIDTH_RE.sub("", _ANY_TAG_RE.sub("", value))


def _close_pattern(name: str) -> "re.Pattern[str]":
    """匹配 ``[name]`` / ``[name=...]`` / ``[/name]``，避免吞掉 ``[namex]``。"""
    return re.compile(
        r"\[(?P<close>/?)" + re.escape(name) + r"(?P<attrs>=[^\[\]]*|)\]",
        re.IGNORECASE,
    )


def _find_close(text: str, start: int, name: str) -> Optional["re.Match[str]"]:
    """从 ``start`` 起找与已打开标签配对的关闭标签（支持同名嵌套）。"""
    pattern = _close_pattern(name)
    depth = 1
    for match in pattern.finditer(text, start):
        if match.group("close"):
            depth -= 1
            if depth == 0:
                return match
        else:
            depth += 1
    return None


def _merge_text_blocks(blocks: List[Block]) -> List[Block]:
    """合并相邻文本块，保持其它块顺序不变。"""
    merged: List[Block] = []
    for block in blocks:
        if block.kind == "text" and merged and merged[-1].kind == "text":
            merged[-1] = Block("text", merged[-1].text + block.text)
        else:
            merged.append(block)
    return merged


def _finalize_blocks(blocks: List[Block]) -> List[Block]:
    """递归清洗文本块（引用子块同样处理）。"""
    result: List[Block] = []
    for block in blocks:
        if block.kind == "text":
            result.append(Block("text", _clean_text_value(block.text)))
        elif block.kind == "quote":
            result.append(Block("quote", "", _finalize_blocks(block.children)))
        else:
            result.append(block)
    return result


def _parse_segments(text: str, depth: int, warnings: List[str]) -> List[Block]:
    """递归解析一段 BBCode 文本，返回顺序 block 列表（文本块可能相邻）。"""
    blocks: List[Block] = []
    pos = 0
    length = len(text)
    while pos < length:
        match = _TAG_RE.search(text, pos)
        if match is None:
            if pos < length:
                blocks.append(Block("text", text[pos:]))
            break
        if match.start() > pos:
            blocks.append(Block("text", text[pos:match.start()]))
        token = match.group(0)
        pos = match.end()

        if match.group("star"):
            blocks.append(Block("text", "- "))
            continue

        name = match.group("name") or ""
        name_lower = name.lower()
        attrs = match.group("attrs") or ""
        if match.group("close"):
            # 多余的关闭标签：只去壳，不影响后续文本。
            continue

        # 表情 [s:1:xxx]（部分标签名会被正则整体吞进 name）。
        if name_lower.startswith("s:") or (name_lower == "s" and attrs.startswith(":")):
            blocks.append(Block("text", "[表情]"))
            continue

        if name_lower == "img":
            close = _find_close(text, pos, "img")
            if close is None:
                _warn(warnings, "[img] 未闭合，按文本处理")
                blocks.append(Block("text", token))
                continue
            url = absolutize_url(_plain_inline(text[pos:close.start()]))
            pos = close.end()
            if url:
                blocks.append(Block("image", url))
            else:
                _warn(warnings, "[img] 内容为空，已忽略")
            continue

        if name_lower == "url":
            close = _find_close(text, pos, "url")
            if close is None:
                _warn(warnings, "[url] 未闭合，按文本处理")
                blocks.append(Block("text", token))
                continue
            inner = text[pos:close.start()]
            pos = close.end()
            href_attr = attrs[1:].strip().strip("\"'") if attrs.startswith("=") else None
            if href_attr is None:
                rendered = absolutize_url(_plain_inline(inner))
            else:
                href = absolutize_url(href_attr)
                label = _plain_inline(inner)
                if not href:
                    rendered = label
                elif not label or label in (href_attr, href):
                    rendered = href
                else:
                    rendered = f"{label} ({href})"
            if rendered:
                blocks.append(Block("link", rendered))
            continue

        if name_lower == "quote":
            close = _find_close(text, pos, "quote")
            if close is None:
                _warn(warnings, "[quote] 未闭合，按文本处理")
                blocks.append(Block("text", token))
                continue
            inner = text[pos:close.start()]
            pos = close.end()
            if depth >= MAX_QUOTE_DEPTH:
                _warn(warnings, f"引用嵌套超过 {MAX_QUOTE_DEPTH} 层，已降级为文本")
                stripped = _strip_all_tags(inner)
                if stripped.strip():
                    blocks.append(Block("text", stripped))
                continue
            children = _merge_text_blocks(_parse_segments(inner, depth + 1, warnings))
            if children:
                blocks.append(Block("quote", "", children))
            else:
                _warn(warnings, "引用内容为空，已忽略")
            continue

        if name_lower == "code":
            close = _find_close(text, pos, "code")
            if close is None:
                _warn(warnings, "[code] 未闭合，按文本处理")
                blocks.append(Block("text", token))
                continue
            blocks.append(Block("text", text[pos:close.start()]))
            pos = close.end()
            continue

        # [pid=...] / [uid=...]：去壳留文；关闭标签会在后续作为多余标签丢弃。
        # 其它样式/未知标签同样“去壳留文”。
        continue
    return blocks


def _clean_edited_phrase(raw: str) -> Optional[str]:
    """清洗编辑标记：去标签、归一空白，保留完整短语。"""
    value = _ANY_TAG_RE.sub("", raw)
    value = html.unescape(value)
    value = _ZERO_WIDTH_RE.sub("", value)
    value = _WHITESPACE_RE.sub(" ", value).strip()
    return value or None


def _extract_edited(blocks: List[Block]) -> Tuple[List[Block], Optional[str]]:
    """从顶层文本块中提取最后一处“本帖最后由 … 编辑”，并把它从正文移除。

    不扫描引用子块：引用里出现的编辑标记属于被引用的楼层。
    """
    last_index = -1
    last_match: Optional["re.Match[str]"] = None
    for index, block in enumerate(blocks):
        if block.kind != "text":
            continue
        for match in _EDITED_RE.finditer(block.text):
            last_index = index
            last_match = match
    if last_match is None or last_index < 0:
        return blocks, None
    phrase = _clean_edited_phrase(last_match.group(0))
    if phrase is None:
        return blocks, None
    result: List[Block] = []
    for index, block in enumerate(blocks):
        if index != last_index:
            result.append(block)
            continue
        remainder = block.text[: last_match.start()] + block.text[last_match.end() :]
        if remainder.strip():
            result.append(Block("text", remainder))
    return result, phrase


def _has_visible_content(blocks: List[Block]) -> bool:
    """判断正文是否有可见内容（纯空白不算）。"""
    for block in blocks:
        if block.kind == "image":
            return True
        if block.kind in ("text", "link"):
            if block.text.strip():
                return True
        elif block.kind == "quote":
            if _has_visible_content(block.children):
                return True
    return False


def bbcode_to_blocks(text: Any) -> Tuple[List[Block], Optional[str], List[str]]:
    """把 NGA 正文 BBCode 转成 ``(blocks, edited, warnings)``。

    只覆盖帖内常见标签（方案附录 C），不追求完整 BBCode 语法；未知 ASCII 标签
    “去壳留文”，中文方括号文本不受影响。
    """
    warnings: List[str] = []
    if not isinstance(text, str) or not text.strip():
        return [], None, warnings

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    blocks = _merge_text_blocks(_parse_segments(normalized, 0, warnings))
    blocks = _finalize_blocks(blocks)
    blocks, edited = _extract_edited(blocks)
    blocks = _merge_text_blocks(blocks)
    return blocks, edited, warnings


# ---------------------------------------------------------------------------
# 缓存解析
# ---------------------------------------------------------------------------


def _parse_timestamp(value: Any) -> Optional[str]:
    """按固定 UTC+8 把时间戳格式化为 ``YYYY-MM-DD HH:MM``（毫秒先降为秒）。"""
    seconds = _as_int(value)
    if seconds is None:
        return None
    if abs(seconds) > _MILLISECOND_THRESHOLD:
        seconds //= 1000
    try:
        moment = datetime.fromtimestamp(seconds, tz=_UTC8)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.strftime(_POST_TIME_FORMAT)


def _format_post_time(post_date: Any, post_timestamp: Any) -> Optional[str]:
    """``postdate`` 原样优先；缺失时用 ``postdatetimestamp`` 按 UTC+8 格式化。"""
    text = _as_clean_str(post_date)
    if text is not None:
        return text
    numeric = _as_int(post_date)
    if numeric is not None:
        return _parse_timestamp(numeric)
    return _parse_timestamp(post_timestamp)


def _build_user_map(raw: Any, warnings: List[str]) -> Dict[int, str]:
    """``__U`` → {authorid: 昵称}，兼容 ``username`` / ``nickname`` / ``name``。"""
    mapping: Dict[int, str] = {}
    if raw is None:
        return mapping
    if not isinstance(raw, dict):
        _warn(warnings, "__U 不是对象，已忽略作者映射")
        return mapping
    for key, value in raw.items():
        author_id = _as_int(key)
        if author_id is None:
            continue
        name: Optional[str] = None
        if isinstance(value, dict):
            for field_name in ("username", "nickname", "name"):
                name = _as_clean_str(value.get(field_name))
                if name is not None:
                    break
        else:
            name = _as_clean_str(value)
        if name is not None:
            mapping[author_id] = name
    return mapping


def _parse_post(
    item: Dict[str, Any],
    index: int,
    user_map: Dict[int, str],
    op_author_id: Optional[int],
    warnings: List[str],
) -> ParsedPost:
    """解析单条 ``__R``；字段级问题写入 ``missing_fields`` / ``warnings``。"""
    missing: List[str] = []

    lou = _as_int(item.get("lou"))
    if lou is None:
        missing.append(MISSING_LOU)
        _warn(warnings, f"第 {index + 1} 条回复缺少楼层号")

    pid = _as_int(item.get("pid"))
    author_id = _as_int(item.get("authorid"))

    author = _as_clean_str(item.get("author"))
    if author is None and author_id is not None:
        author = user_map.get(author_id)
    if author is None:
        missing.append(MISSING_AUTHOR)
        _warn(warnings, f"第 {index + 1} 条回复缺少作者")

    posted_at = _format_post_time(item.get("postdate"), item.get("postdatetimestamp"))
    if posted_at is None:
        missing.append(MISSING_TIME)
        _warn(warnings, f"第 {index + 1} 条回复缺少发帖时间")

    raw_content = item.get("content")
    blocks, edited, block_warnings = bbcode_to_blocks(
        raw_content if isinstance(raw_content, str) else ""
    )
    for block_warning in block_warnings:
        _warn(warnings, f"第 {index + 1} 条回复：{block_warning}")
    if not _has_visible_content(blocks):
        missing.append(MISSING_BODY)
        _warn(warnings, f"第 {index + 1} 条回复正文为空")

    is_op = (
        op_author_id is not None and author_id is not None and author_id == op_author_id
    )
    return ParsedPost(
        lou=lou,
        pid=pid,
        author=author,
        author_id=author_id,
        posted_at=posted_at,
        blocks=blocks,
        edited=edited,
        is_op=is_op,
        missing_fields=missing,
    )


def _skipped_page(
    cache: Dict[str, Any],
    tid: int,
    page: int,
    status: str,
    page_type: str,
    reason: str,
) -> ParsedPage:
    """构造 skipped 页；字段尽力从外层读取，问题只体现在 ``skip_reason``。"""
    return ParsedPage(
        tid=tid,
        page=page,
        page_type=page_type,
        status=status,
        title=None,
        op_author=None,
        total_pages=_as_int(cache.get("total_pages")),
        fetcher_version=_as_clean_str(cache.get("fetcher_version")),
        req_id=_as_clean_str(cache.get("req_id")),
        fetched_at=_as_clean_str(cache.get("fetched_at")),
        posts=[],
        warnings=[],
        missing_fields={},
        skipped=True,
        skip_reason=reason,
    )


def parse_cache(cache: dict) -> ParsedPage:
    """解析单页缓存 dict，返回 ``ParsedPage``；任何业务异常都降级为 skipped/告警。"""
    if not isinstance(cache, dict):
        return ParsedPage(
            tid=0,
            page=0,
            page_type="unknown",
            status="unknown",
            title=None,
            op_author=None,
            total_pages=None,
            fetcher_version=None,
            req_id=None,
            fetched_at=None,
            posts=[],
            warnings=[],
            missing_fields={},
            skipped=True,
            skip_reason="缓存记录不是 JSON 对象",
        )

    tid_value = _as_int(cache.get("tid"))
    page_value = _as_int(cache.get("page"))
    tid = tid_value if tid_value is not None else 0
    page = page_value if page_value is not None else 0
    status = _as_clean_str(cache.get("status")) or "unknown"
    page_type = _as_clean_str(cache.get("page_type")) or "unknown"
    content = cache.get("content")
    source = _as_clean_str(content.get("source")) if isinstance(content, dict) else None

    # 1. 外层校验：只有 ok/thread/json 才继续解析。
    if status != "ok" or page_type != "thread" or source != "json":
        reason = (
            "外层不满足可解析条件："
            f"status={status!r}, page_type={page_type!r}, content.source={source!r}"
        )
        return _skipped_page(cache, tid, page, status, page_type, reason)

    # 2. 内层 data。
    data = content.get("data") if isinstance(content, dict) else None
    if not isinstance(data, dict):
        return _skipped_page(
            cache, tid, page, status, page_type, "content.data 缺失或不是 JSON 对象"
        )

    warnings: List[str] = []
    total_pages = _as_int(cache.get("total_pages"))
    fetcher_version = _as_clean_str(cache.get("fetcher_version"))
    fetched_at = _as_clean_str(cache.get("fetched_at"))
    req_id = _as_clean_str(cache.get("req_id"))
    if total_pages is None:
        _warn(warnings, "总页数缺失：外层 total_pages 非法或为空")
    if fetched_at is None:
        _warn(warnings, "抓取时间缺失：外层 fetched_at 为空")
    if fetcher_version is None:
        _warn(warnings, "版本缺失：外层 fetcher_version 为空")

    # 3. __U 作者映射。
    user_map = _build_user_map(data.get("__U"), warnings)

    # 4. __T 标题/楼主。
    thread = data.get("__T")
    if not isinstance(thread, dict):
        _warn(warnings, "__T 缺失或不是对象")
        thread = {}
    title = _clean_title(thread.get("subject"))
    if title is None:
        _warn(warnings, "标题缺失：__T.subject 为空")
    op_author_id = _as_int(thread.get("authorid"))
    op_author = _as_clean_str(thread.get("author"))
    if op_author is None and op_author_id is not None:
        op_author = user_map.get(op_author_id)
    if op_author is None:
        _warn(warnings, "楼主缺失：__T.author 为空")

    # 5. 遍历 __R。
    replies = data.get("__R")
    if not isinstance(replies, list):
        _warn(warnings, "__R 缺失或不是数组，按无回复处理")
        replies = []
    posts: List[ParsedPost] = []
    counts: Dict[str, int] = {}
    for index, item in enumerate(replies):
        if not isinstance(item, dict):
            _warn(warnings, f"第 {index + 1} 条回复不是对象，已跳过")
            continue
        post = _parse_post(item, index, user_map, op_author_id, warnings)
        posts.append(post)
        for field_name in post.missing_fields:
            counts[field_name] = counts.get(field_name, 0) + 1

    return ParsedPage(
        tid=tid,
        page=page,
        page_type=page_type,
        status=status,
        title=title,
        op_author=op_author,
        total_pages=total_pages,
        fetcher_version=fetcher_version,
        req_id=req_id,
        fetched_at=fetched_at,
        posts=posts,
        warnings=warnings,
        missing_fields=counts,
        skipped=False,
        skip_reason=None,
    )


__all__ = [
    "Block",
    "ParsedPost",
    "ParsedPage",
    "parse_cache",
    "bbcode_to_blocks",
    "absolutize_url",
    "MAX_QUOTE_DEPTH",
    "MISSING_LOU",
    "MISSING_AUTHOR",
    "MISSING_TIME",
    "MISSING_BODY",
]
