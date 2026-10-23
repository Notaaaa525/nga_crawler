"""HTTP 抓取与页缓存（唯一直接使用 :mod:`urllib.request` 的模块）。

职责：

- 按契约构造 ``read.php`` 请求并抓取单页；
- 解压/解码/解析 JSON，计算分页，检测“越界夹回”；
- 成功页与错误页（标记缓存）的原子写入；
- 缓存读取与可复用判断。

不做：页间 ``sleep`` 与缓存跳过（属于 engine），业务字段提取（属于 parser）。
"""
from __future__ import annotations

import gzip
import json
import logging
import os
import re
import socket
import time
import zlib
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config.auth import load_auth
from config.runtime import (
    BASE_URL,
    FETCHER_VERSION,
    USER_AGENT,
    RuntimeConfig,
    resolve_runtime,
)
from log import new_req_id, request_context

__all__ = [
    "CrawlError",
    "AuthError",
    "RateLimitError",
    "PageClampedError",
    "PageError",
    "CacheWriteError",
    "build_url",
    "fetch_page",
    "load_cached_page",
    "is_usable_cache",
]

logger = logging.getLogger(__name__)

#: 线性退避上限（秒）；退避秒数 = ``min(2 * attempt, 8)``。
MAX_BACKOFF_SECONDS = 8
#: 错误页摘要上限（字节）。
MAX_EXCERPT_BYTES = 2048
#: ``os.replace`` 失败后的重试次数（首次之外）。
ATOMIC_WRITE_RETRIES = 3
#: 每次 ``os.replace`` 重试前的等待秒数。
ATOMIC_WRITE_RETRY_DELAY = 0.2

ACCEPT = "application/json, text/plain, */*"
ACCEPT_ENCODING = "identity"
REFERER = "https://bbs.nga.cn/"

#: 瞬时错误（网络层）。``HTTPError`` 会先被单独捕获，故不在此列。
_TRANSIENT_EXCEPTIONS = (URLError, OSError, TimeoutError, ConnectionError, socket.timeout)

#: 登录墙的中文强特征（HTML 与 JSON 共用；JSON 额外看 code/msg/data.__MESSAGE）。
_LOGIN_TEXT_HINTS = ("未登录", "请登录", "登录后", "ngapassport")
_LOGIN_FORM_RE = re.compile(r"<form[^>]*(?:login|passport)[^>]*>", re.IGNORECASE)
#: 永久主题错误特征；命中即致命、不重试。
_PERMANENT_HINTS = (
    "主题不存在",
    "帖子不存在",
    "不存在或已删除",
    "已删除",
    "审核",
    "无权",
    "锁定",
    "权限不足",
    "禁止访问",
)

_CHARSET_RE = re.compile(r"charset\s*=\s*[\"']?([A-Za-z0-9_\-]+)", re.IGNORECASE)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# 异常层次（§9.9）
# ---------------------------------------------------------------------------


class CrawlError(Exception):
    """抓取失败的基类。"""


class AuthError(CrawlError):
    """认证失效 / 登录墙（Cookie 过期或未登录）。"""


class RateLimitError(CrawlError):
    """服务端限流（HTTP 429 / JSON code=2048 / 3xx 无 Location）。"""


class PageClampedError(CrawlError):
    """请求页超出总页数、被服务端夹回最后一页；不写缓存。"""

    def __init__(self, requested: int, current: int, total: int) -> None:
        self.requested = requested
        self.current = current
        self.total = total
        super().__init__(
            f"请求页 {requested} 被夹回第 {current} 页（总页数 {total}）"
        )


class PageError(CrawlError):
    """页面级错误；``fatal=True`` 表示永久错误（如主题不存在/无权）。"""

    def __init__(
        self,
        page: Optional[int] = None,
        status: Optional[int] = None,
        message: str = "",
        fatal: bool = False,
    ) -> None:
        self.page = page
        self.status = status
        self.message = message
        self.fatal = bool(fatal)
        super().__init__(self._describe())

    def _describe(self) -> str:
        parts = []
        if self.page is not None:
            parts.append(f"page={self.page}")
        if self.status is not None:
            parts.append(f"status={self.status}")
        if self.message:
            parts.append(str(self.message))
        text = " ".join(parts) or "页面抓取失败"
        return f"{text}（致命）" if self.fatal else text


class CacheWriteError(CrawlError):
    """缓存原子写入失败（目录/写入/``os.replace`` 重试耗尽）。"""


# ---------------------------------------------------------------------------
# 内部信号：把“响应语义”与“异常/缓存动作”解耦，便于集中处理
# ---------------------------------------------------------------------------


class _LoginWall(Exception):
    """响应判定为登录墙。"""


class _RateLimited(Exception):
    """响应判定为限流。"""


class _PermanentError(Exception):
    """响应判定为永久主题错误。"""


class _MalformedResponse(Exception):
    """响应无法解析/结构缺失（可重试的“畸形响应”）。"""

    def __init__(self, message: str, excerpt: str = "") -> None:
        self.message = message
        self.excerpt = excerpt
        super().__init__(message)


# ---------------------------------------------------------------------------
# URL / 时间 / 缓存路径
# ---------------------------------------------------------------------------


def build_url(tid: int, page: int, *, cfg: Optional[RuntimeConfig] = None) -> str:
    """构造请求 URL；参数顺序固定为 ``tid``、``page``、``__output``。

    例：``https://bbs.nga.cn/read.php?tid=123&page=1&__output=11``
    """
    base = getattr(cfg, "base_url", None) or BASE_URL
    base = str(base).rstrip("?")
    query = urlencode({"tid": int(tid), "page": int(page), "__output": 11})
    return f"{base}?{query}"


def _now_iso() -> str:
    """本地时区 ISO8601（含偏移、秒精度），如 ``2026-10-23T12:34:56+08:00``。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _cache_path(cache_dir: Any, tid: int, page: int) -> Path:
    """缓存路径：``cache_dir/{tid}/page_{page:03d}.json``。"""
    return Path(cache_dir) / str(tid) / f"page_{int(page):03d}.json"


# ---------------------------------------------------------------------------
# 缓存：原子写 / 读取 / 可复用判断
# ---------------------------------------------------------------------------


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _atomic_write_json(path: Path, record: Dict[str, Any]) -> None:
    """同目录 tmp → flush + fsync → ``os.replace``；失败重试 3 次后抛错。"""
    payload = json.dumps(record, ensure_ascii=False, indent=1).encode("utf-8")
    directory = path.parent
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CacheWriteError(f"无法创建缓存目录 {directory}：{exc}") from exc

    tmp_path = path.with_name(path.name + ".tmp")
    try:
        with open(tmp_path, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        _remove_quietly(tmp_path)
        raise CacheWriteError(f"无法写入缓存临时文件 {tmp_path}：{exc}") from exc

    last_error: Optional[OSError] = None
    # 首次尝试 + 最多 3 次重试（Windows 被杀软/占用短暂锁定）。
    for attempt in range(1, ATOMIC_WRITE_RETRIES + 2):
        try:
            os.replace(tmp_path, path)
            return
        except OSError as exc:
            last_error = exc
            if attempt <= ATOMIC_WRITE_RETRIES:
                time.sleep(ATOMIC_WRITE_RETRY_DELAY)

    _remove_quietly(tmp_path)
    raise CacheWriteError(
        f"无法替换缓存文件 {path}（已重试 {ATOMIC_WRITE_RETRIES} 次）：{last_error}"
    ) from last_error


def load_cached_page(tid: int, page: int, cache_dir: Any) -> Optional[Dict[str, Any]]:
    """读取页缓存；不存在/损坏返回 ``None``（按缺失处理，只记 WARNING）。"""
    path = _cache_path(cache_dir, tid, page)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            record = json.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("缓存读取失败 page=%s path=%s：%s", page, path, exc)
        return None
    if not isinstance(record, dict):
        logger.warning("缓存结构非法（非对象） page=%s path=%s", page, path)
        return None
    return record


def is_usable_cache(record: Dict[str, Any], tid: int, page: int) -> bool:
    """严格按 §7.5 判断缓存页是否可复用（只认成功 thread 页）。"""
    if not isinstance(record, dict):
        return False
    content = record.get("content")
    return (
        record.get("tid") == tid
        and record.get("page") == page
        and record.get("status") == "ok"
        and record.get("page_type") == "thread"
        and isinstance(record.get("total_pages"), int)
        and isinstance(content, dict)
        and isinstance(content.get("data"), dict)
    )


# ---------------------------------------------------------------------------
# 响应读取 / 解码 / 特征判断
# ---------------------------------------------------------------------------


def _to_int(value: Any) -> Optional[int]:
    """尽量把配置/JSON 中的值转成 int；失败返回 ``None``。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        text = str(value).strip()
        try:
            return int(text)
        except (TypeError, ValueError):
            return None


def _get_header(headers: Any, name: str) -> Optional[str]:
    """大小写不敏感地读取响应头（兼容 dict 与 ``email.message``）。"""
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        for key in (name, name.lower()):
            try:
                value = getter(key)
            except (KeyError, TypeError):
                value = None
            if value is not None:
                return str(value)
    items = getattr(headers, "items", None)
    if callable(items):
        try:
            lowered = name.lower()
            for key, value in items():
                if str(key).lower() == lowered:
                    return str(value)
        except Exception:  # pragma: no cover - 仅防御异常 headers 实现
            return None
    return None


def _extract_charset(headers: Any) -> Optional[str]:
    content_type = _get_header(headers, "Content-Type") or ""
    match = _CHARSET_RE.search(content_type)
    return match.group(1) if match else None


def _decode_body(raw: Any, headers: Any) -> str:
    """按 §6.3 解压 + 选编码，去除 BOM 与首尾空白。"""
    data = bytes(raw or b"")

    encoding = (_get_header(headers, "Content-Encoding") or "").strip().lower()
    try:
        if data and encoding in ("gzip", "x-gzip"):
            data = gzip.decompress(data)
        elif data and encoding == "deflate":
            try:
                data = zlib.decompress(data)
            except zlib.error:
                data = zlib.decompress(data, -zlib.MAX_WBITS)
    except (OSError, zlib.error) as exc:
        raise _MalformedResponse(f"响应解压失败：{exc}") from exc

    charset = _extract_charset(headers)
    text: Optional[str] = None
    if charset:
        try:
            text = data.decode(charset, errors="replace")
        except LookupError:  # 未知 charset：回退到标准顺序
            text = None
    if text is None:
        for candidate in ("utf-8-sig", "gb18030", "latin-1"):
            try:
                text = data.decode(candidate)
                break
            except UnicodeDecodeError:
                continue
        if text is None:  # pragma: no cover - latin-1 永不失败，仅防御
            text = data.decode("latin-1", errors="replace")

    return text.lstrip("\ufeff").strip()


def _read_response(response: Any) -> Tuple[Optional[int], Any, bytes]:
    status = _to_int(getattr(response, "status", None))
    if status is None:
        getcode = getattr(response, "getcode", None)
        if callable(getcode):
            status = _to_int(getcode())
    headers = getattr(response, "headers", None)
    try:
        body = response.read()
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover - 关闭失败不影响结果
                pass
    return status, headers, bytes(body or b"")


def _read_http_error(exc: HTTPError) -> Tuple[Optional[int], Any, bytes]:
    status = _to_int(getattr(exc, "code", None))
    if status is None:
        status = _to_int(getattr(exc, "status", None))
    headers = getattr(exc, "headers", None)
    try:
        body = exc.read()
    except Exception:  # pragma: no cover - 错误体不可读时按空处理
        body = b""
    try:
        exc.close()
    except Exception:  # pragma: no cover
        pass
    return status, headers, bytes(body or b"")


def _looks_like_login_html(text: str) -> bool:
    """HTML 登录墙组合特征，避免把正文里的“登录”二字误判。"""
    lowered = text.lower()
    if any(hint in lowered for hint in _LOGIN_TEXT_HINTS):
        return True
    if _LOGIN_FORM_RE.search(text):
        return True
    has_password = (
        'type="password"' in lowered
        or "type='password'" in lowered
        or 'name="password"' in lowered
        or "passwd" in lowered
    )
    return has_password and ("login" in lowered or "登录" in text)


def _match_permanent_hint(text: str) -> Optional[str]:
    if not text:
        return None
    lowered = text.lower()
    for hint in _PERMANENT_HINTS:
        if hint.lower() in lowered:
            return hint
    return None


def _json_message(payload: Dict[str, Any]) -> str:
    """汇总 JSON 中可能携带服务端消息的字段（§6.7：code/msg/data.__MESSAGE）。"""
    parts = []
    for key in ("msg", "message", "error", "__MESSAGE", "__MSG"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("__MESSAGE", "__MSG", "msg", "message", "error"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                parts.append(value.strip())
    elif isinstance(data, str) and data.strip():
        parts.append(data.strip())
    return " ".join(parts)


def _message_is_login(message: str) -> bool:
    if not message:
        return False
    lowered = message.lower()
    return any(hint in lowered for hint in _LOGIN_TEXT_HINTS) or "登录" in message


def _is_rate_limit_code(code: Any) -> bool:
    return _to_int(code) == 2048


def _code_is_ok(code: Any) -> bool:
    return code is None or _to_int(code) == 0


def _interpret_payload(
    payload: Any,
    *,
    page: int,
    status: Optional[int],
    excerpt: str,
) -> Dict[str, Any]:
    """判定 JSON 语义；成功返回内层 ``data``，否则抛内部信号。"""
    if not isinstance(payload, dict):
        raise _MalformedResponse("JSON 顶层不是对象", excerpt=excerpt)

    code = payload.get("code")
    message = _json_message(payload)
    nested = payload.get("data")
    data = nested if isinstance(nested, dict) else payload

    if _is_rate_limit_code(code):
        raise _RateLimited("JSON code=2048（请求过于频繁）")
    if _message_is_login(message):
        raise _LoginWall(f"HTTP {status} / 未登录")
    permanent = _match_permanent_hint(message)
    if permanent is not None:
        raise _PermanentError(f"主题不可访问（{permanent}）")

    if _code_is_ok(code) and isinstance(data.get("__R"), list):
        return data

    raise _MalformedResponse(f"响应结构异常（code={code!r}）", excerpt=excerpt)


def _interpret_body(text: str, *, page: int, status: Optional[int]) -> Dict[str, Any]:
    """区分 JSON / HTML，返回成功页的内层 ``data``。"""
    stripped = text.strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            payload = json.loads(stripped)
        except ValueError as exc:
            raise _MalformedResponse("JSON 解析失败", excerpt=stripped) from exc
        return _interpret_payload(payload, page=page, status=status, excerpt=stripped)

    # 非 JSON：只可能是登录墙 / 主题错误页 / 畸形响应。
    permanent = _match_permanent_hint(text)
    if permanent is not None:
        raise _PermanentError(f"主题不可访问（{permanent}）")
    if _looks_like_login_html(text):
        raise _LoginWall(f"HTTP {status} / 未登录")
    raise _MalformedResponse("响应不是有效 JSON", excerpt=text)


def _meta_value(meta: Any, index: int) -> Optional[int]:
    """读取 ``__PAGE`` 的第 ``index`` 位（1/2/3），兼容 dict 与 list。"""
    if isinstance(meta, dict):
        for key in (str(index), index):
            if key in meta:
                return _to_int(meta[key])
        return None
    if isinstance(meta, (list, tuple)) and 0 <= index - 1 < len(meta):
        return _to_int(meta[index - 1])
    return None


def _compute_pagination(
    data: Dict[str, Any],
    tid: int,
    page: int,
    cfg: RuntimeConfig,
) -> Tuple[int, int, Optional[str]]:
    """计算 ``(total, current, next_url)``，按 §6.5 兜底。"""
    page_meta = data.get("__PAGE")
    total = _meta_value(page_meta, 1)
    current = _meta_value(page_meta, 2)

    if total is None:
        rows = _to_int(data.get("__ROWS"))
        per_page = _to_int(data.get("__R__ROWS_PAGE"))
        if per_page is None:
            per_page = _to_int(data.get("__ROWS_PAGE"))
        if rows is not None and per_page is not None and per_page > 0:
            total = (rows + per_page - 1) // per_page

    if current is None:
        current = page

    if total is None:
        total = page
        current = page
        logger.warning("无法计算总页数，按单页处理 tid=%s page=%s", tid, page)

    total = max(1, total)
    next_url = build_url(tid, page + 1, cfg=cfg) if page < total else None
    return total, current, next_url


# ---------------------------------------------------------------------------
# 缓存记录组装
# ---------------------------------------------------------------------------


def _clean_excerpt(text: str, limit: int = MAX_EXCERPT_BYTES) -> str:
    """去 script/style → 去标签 → 空白归一 → 截断到 ``limit`` 字节。"""
    if not text:
        return ""
    cleaned = _SCRIPT_STYLE_RE.sub(" ", text)
    cleaned = _TAG_RE.sub(" ", cleaned)
    cleaned = unescape(cleaned)
    cleaned = _WHITESPACE_RE.sub(" ", cleaned).strip()
    raw = cleaned.encode("utf-8")
    if len(raw) > limit:
        cleaned = raw[:limit].decode("utf-8", errors="ignore").strip()
    return cleaned


def _build_success_record(
    *,
    tid: int,
    page: int,
    url: str,
    req_id: str,
    cfg: RuntimeConfig,
    data: Dict[str, Any],
    total: int,
    current: int,
    next_url: Optional[str],
) -> Dict[str, Any]:
    return {
        "tid": tid,
        "page": page,
        "url": url,
        "fetched_at": _now_iso(),
        "status": "ok",
        "req_id": req_id,
        "page_type": "thread",
        "total_pages": int(total),
        "current_page": int(current),
        "next_url": next_url,
        "fetcher_version": getattr(cfg, "fetcher_version", FETCHER_VERSION),
        "content": {"source": "json", "data": data},
    }


def _build_marker_record(
    *,
    tid: int,
    page: int,
    url: str,
    req_id: str,
    cfg: RuntimeConfig,
    status: str,
    page_type: str,
    message: str,
    body_text: str = "",
    total_pages: Optional[int] = None,
    current_page: Optional[int] = None,
) -> Dict[str, Any]:
    has_body = bool(body_text and body_text.strip())
    return {
        "tid": tid,
        "page": page,
        "url": url,
        "fetched_at": _now_iso(),
        "status": status,
        "req_id": req_id,
        "page_type": page_type,
        "total_pages": total_pages,
        "current_page": current_page,
        "next_url": None,
        "fetcher_version": getattr(cfg, "fetcher_version", FETCHER_VERSION),
        "content": {
            "source": "html" if has_body else "none",
            "message": message,
            "excerpt": _clean_excerpt(body_text) if has_body else "",
        },
    }


def _write_marker_cache(
    *,
    cfg: RuntimeConfig,
    tid: int,
    page: int,
    url: str,
    req_id: str,
    status: str,
    page_type: str,
    message: str,
    body_text: str = "",
    total_pages: Optional[int] = None,
    current_page: Optional[int] = None,
) -> None:
    """写标记缓存（``status != ok``）；失败抛 :class:`CacheWriteError`。"""
    record = _build_marker_record(
        tid=tid,
        page=page,
        url=url,
        req_id=req_id,
        cfg=cfg,
        status=status,
        page_type=page_type,
        message=message,
        body_text=body_text,
        total_pages=total_pages,
        current_page=current_page,
    )
    _atomic_write_json(_cache_path(cfg.cache_dir, tid, page), record)


# ---------------------------------------------------------------------------
# 请求头
# ---------------------------------------------------------------------------


def _build_request(url: str, cfg: RuntimeConfig, cookie: str) -> Request:
    headers = {
        "Cookie": cookie,
        "User-Agent": getattr(cfg, "user_agent", USER_AGENT),
        "Accept": ACCEPT,
        "Accept-Encoding": ACCEPT_ENCODING,
        "Referer": REFERER,
    }
    return Request(url, headers=headers, method="GET")


# ---------------------------------------------------------------------------
# 单页抓取
# ---------------------------------------------------------------------------


def fetch_page(
    tid: int,
    page: int,
    *,
    cfg: Optional[RuntimeConfig] = None,
    req_id: Optional[str] = None,
) -> Dict[str, Any]:
    """抓取单页并返回缓存记录（成功页已原子写入；错误页按类型处理）。

    异常语义：

    - :class:`AuthError`：认证失效/登录墙；已写 ``auth_error/login_wall`` 标记缓存；
    - :class:`RateLimitError`：限流；**不写缓存**；
    - :class:`PageClampedError`：请求页越界被夹回；**不写缓存**；
    - :class:`PageError`：页面错误；``fatal=True`` 为永久错误，均已写标记缓存；
    - :class:`CrawlError`：网络重试耗尽（无响应）；**不写缓存**；
    - :class:`CacheWriteError`：缓存落盘失败。
    """
    if cfg is None:
        cfg = resolve_runtime(None)
    if req_id is None:
        req_id = new_req_id(tid, page)
    url = build_url(tid, page, cfg=cfg)

    with request_context(req_id):
        auth = load_auth()
        request = _build_request(url, cfg, auth.cookie)

        max_retries = max(0, int(getattr(cfg, "max_retries", 0)))
        attempts_total = max_retries + 1

        def wait_before_retry(attempt: int, reason: str) -> bool:
            """未到上限则等待并返回 True；否则返回 False（由调用方收尾）。"""
            if attempt >= attempts_total:
                return False
            delay = min(2 * attempt, MAX_BACKOFF_SECONDS)
            logger.warning(
                "瞬时失败 page=%s attempt=%s/%s 原因=%s 退避=%ss",
                page,
                attempt,
                attempts_total,
                reason,
                delay,
            )
            time.sleep(delay)
            return True

        for attempt in range(1, attempts_total + 1):
            started = time.monotonic()
            try:
                response = urlopen(request, timeout=getattr(cfg, "timeout", 30.0))
                status, headers, body = _read_response(response)
            except HTTPError as exc:
                status, headers, body = _read_http_error(exc)
            except _TRANSIENT_EXCEPTIONS as exc:
                reason = f"{exc.__class__.__name__}: {exc}"
                logger.warning("请求失败 page=%s attempt=%s：%s", page, attempt, reason)
                if wait_before_retry(attempt, reason):
                    continue
                raise CrawlError(
                    f"请求 {url} 失败（已重试 {max_retries} 次）：{reason}"
                ) from exc

            elapsed = time.monotonic() - started
            logger.debug(
                "请求 page=%s url=%s status=%s 耗时=%.3fs",
                page,
                url,
                status,
                elapsed,
            )

            if status is not None and 200 <= status < 300:
                try:
                    text = _decode_body(body, headers)
                except _MalformedResponse as exc:
                    if wait_before_retry(attempt, exc.message):
                        continue
                    _write_marker_cache(
                        cfg=cfg,
                        tid=tid,
                        page=page,
                        url=url,
                        req_id=req_id,
                        status="error",
                        page_type="unknown",
                        message=exc.message,
                        body_text="",
                    )
                    raise PageError(
                        page=page, status=status, message=exc.message, fatal=False
                    ) from exc

                try:
                    data = _interpret_body(text, page=page, status=status)
                except _LoginWall as exc:
                    message = str(exc) or "未登录"
                    _write_marker_cache(
                        cfg=cfg,
                        tid=tid,
                        page=page,
                        url=url,
                        req_id=req_id,
                        status="auth_error",
                        page_type="login_wall",
                        message=message,
                        body_text=text,
                    )
                    raise AuthError(message) from exc
                except _RateLimited as exc:
                    logger.warning("限流检测 page=%s status=%s：%s", page, status, exc)
                    raise RateLimitError(str(exc)) from exc
                except _PermanentError as exc:
                    message = str(exc)
                    _write_marker_cache(
                        cfg=cfg,
                        tid=tid,
                        page=page,
                        url=url,
                        req_id=req_id,
                        status="error",
                        page_type="error",
                        message=message,
                        body_text=text,
                    )
                    raise PageError(
                        page=page, status=status, message=message, fatal=True
                    ) from exc
                except _MalformedResponse as exc:
                    if wait_before_retry(attempt, exc.message):
                        continue
                    _write_marker_cache(
                        cfg=cfg,
                        tid=tid,
                        page=page,
                        url=url,
                        req_id=req_id,
                        status="error",
                        page_type="unknown",
                        message=exc.message,
                        body_text=exc.excerpt or text,
                    )
                    raise PageError(
                        page=page, status=status, message=exc.message, fatal=False
                    ) from exc

                total, current, next_url = _compute_pagination(data, tid, page, cfg)
                if current != page:
                    if page > total:
                        # 越界夹回：不写缓存，engine 据此结束区间。
                        raise PageClampedError(
                            requested=page, current=current, total=total
                        )
                    message = (
                        f"页码异常：请求 {page}，服务端返回第 {current} 页"
                        f"（总页数 {total}）"
                    )
                    _write_marker_cache(
                        cfg=cfg,
                        tid=tid,
                        page=page,
                        url=url,
                        req_id=req_id,
                        status="error",
                        page_type="unknown",
                        message=message,
                        total_pages=total,
                        current_page=current,
                    )
                    raise PageError(
                        page=page, status=status, message=message, fatal=False
                    )

                record = _build_success_record(
                    tid=tid,
                    page=page,
                    url=url,
                    req_id=req_id,
                    cfg=cfg,
                    data=data,
                    total=total,
                    current=current,
                    next_url=next_url,
                )
                _atomic_write_json(_cache_path(cfg.cache_dir, tid, page), record)
                logger.debug("成功 page=%s total=%s", page, total)
                return record

            # ---- 非 2xx：先尽力解码响应体用于摘要 ----
            text = ""
            try:
                text = _decode_body(body, headers)
            except _MalformedResponse:
                text = ""

            if status is None:
                reason = "响应缺少 HTTP 状态码"
                if wait_before_retry(attempt, reason):
                    continue
                _write_marker_cache(
                    cfg=cfg,
                    tid=tid,
                    page=page,
                    url=url,
                    req_id=req_id,
                    status="error",
                    page_type="unknown",
                    message=reason,
                )
                raise PageError(page=page, status=None, message=reason, fatal=False)

            if status in (401, 403):
                message = f"HTTP {status} / 未登录"
                _write_marker_cache(
                    cfg=cfg,
                    tid=tid,
                    page=page,
                    url=url,
                    req_id=req_id,
                    status="auth_error",
                    page_type="login_wall",
                    message=message,
                    body_text=text,
                )
                raise AuthError(message)

            if status == 429:
                logger.warning("限流检测 page=%s status=%s", page, status)
                raise RateLimitError(f"HTTP 429（page={page}）")

            if 300 <= status < 400:
                location = _get_header(headers, "Location")
                if not location:
                    logger.warning(
                        "限流检测 page=%s status=%s（3xx 无 Location）", page, status
                    )
                    raise RateLimitError(f"HTTP {status} 无 Location（page={page}）")
                if wait_before_retry(attempt, f"异常重定向 HTTP {status}"):
                    continue
                message = f"HTTP {status} / 异常重定向"
                _write_marker_cache(
                    cfg=cfg,
                    tid=tid,
                    page=page,
                    url=url,
                    req_id=req_id,
                    status="error",
                    page_type="unknown",
                    message=message,
                    body_text=text,
                )
                raise PageError(page=page, status=status, message=message, fatal=False)

            if status == 404:
                message = "HTTP 404 / 主题不存在或已删除"
                _write_marker_cache(
                    cfg=cfg,
                    tid=tid,
                    page=page,
                    url=url,
                    req_id=req_id,
                    status="error",
                    page_type="error",
                    message=message,
                    body_text=text,
                )
                raise PageError(page=page, status=404, message=message, fatal=True)

            if 500 <= status < 600:
                message = f"HTTP {status}"
                if wait_before_retry(attempt, message):
                    continue
                _write_marker_cache(
                    cfg=cfg,
                    tid=tid,
                    page=page,
                    url=url,
                    req_id=req_id,
                    status="error",
                    page_type="error",
                    message=message,
                    body_text=text,
                )
                raise PageError(
                    page=page, status=status, message=message, fatal=False
                )

            # 其余 4xx（以及非常规状态码）：非致命错误页，不重试。
            message = f"HTTP {status}"
            _write_marker_cache(
                cfg=cfg,
                tid=tid,
                page=page,
                url=url,
                req_id=req_id,
                status="error",
                page_type="error",
                message=message,
                body_text=text,
            )
            raise PageError(page=page, status=status, message=message, fatal=False)

        # 理论上不可达：循环内所有分支都会 return/raise/continue。
        raise CrawlError(f"请求 {url} 未得到有效响应")  # pragma: no cover
