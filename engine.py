"""编排层：两个循环（crawl / parse）、区间解析、状态与输出。

依赖方向只向下：``cli → engine → crawler/nga_parser/render/config/log``。
engine 不直接发 HTTP，所有网络请求都经 ``crawler.fetch_page``。
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

import crawler
import nga_parser as parser_module
import render
from config import auth as auth_config
from config import runtime as runtime_config
from config import targets as targets_config
from log import new_req_id, request_context, setup_logging


class StateError(RuntimeError):
    """``state.json`` 原子写失败。"""


STATE_VERSION = 1
STATE_FILENAME = "state.json"

_PAGE_RANGE_RE = re.compile(r"^(\d+)(?:-(\d*))?$")


@dataclass(frozen=True)
class PageRange:
    """抓取/解析区间；``end is None`` 表示终点待定。"""

    start: int
    end: Optional[int] = None

    @property
    def empty(self) -> bool:
        return self.end is not None and self.end < self.start


# ---------------------------------------------------------------------------
# 区间解析
# ---------------------------------------------------------------------------


def parse_page_range(spec: str) -> PageRange:
    """纯语法解析：``all`` / ``N`` / ``A-B`` / ``A-``。

    非法输入抛 :class:`ValueError`（由 CLI 映射为退出码 1）。
    """
    if not isinstance(spec, str):
        raise ValueError(f"页码区间必须为字符串：{spec!r}")
    text = spec.strip()
    if not text:
        raise ValueError("页码区间不能为空")
    if text.lower() == "all":
        return PageRange(1, None)
    match = _PAGE_RANGE_RE.match(text)
    if match is None:
        raise ValueError(f"无法解析页码区间：{spec!r}（支持 all / N / A-B / A-）")
    start = int(match.group(1))
    if start <= 0:
        raise ValueError(f"页码必须为正整数：{spec!r}")
    end_text = match.group(2)
    if end_text is None:
        return PageRange(start, start)
    if end_text == "":
        return PageRange(start, None)
    end = int(end_text)
    if end <= 0:
        raise ValueError(f"页码必须为正整数：{spec!r}")
    if end < start:
        raise ValueError(f"区间终点不能小于起点：{spec!r}")
    return PageRange(start, end)


def resolve_page_range(
    spec: Optional[str] = None,
    *,
    total_pages: Optional[int] = None,
    max_pages: int = 0,
    failed_pages: Optional[Sequence[int]] = None,
    last_page: int = 0,
    force: bool = False,
) -> PageRange:
    """结合显式区间、state 与 ``max_pages`` 计算本次抓取区间。

    - 显式 ``spec`` 优先，完全覆盖 state；
    - 未给 ``spec`` 时：``force`` 从 1 开始，否则从最早失败页或 ``last_page+1`` 开始；
    - ``total_pages`` 已知时截断到 ``[1, total_pages]``；``start > total`` 返回空区间。
    """
    if spec is not None:
        page_range = parse_page_range(spec)
    elif force:
        page_range = PageRange(1, None)
    else:
        failed = sorted(int(page) for page in (failed_pages or []))
        if failed:
            page_range = PageRange(failed[0], None)
        else:
            page_range = PageRange(int(last_page) + 1, None)

    start = page_range.start
    end = page_range.end

    if max_pages and max_pages > 0:
        if end is None:
            end = start + max_pages - 1
        else:
            end = min(end, start + max_pages - 1)

    if total_pages is not None:
        total = max(1, int(total_pages))
        if start > total:
            return PageRange(start, start - 1)
        if end is None:
            end = total
        elif end > total:
            end = total

    return PageRange(start, end)


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------


def _empty_state() -> Dict[str, Any]:
    return {"version": STATE_VERSION, "threads": {}}


def load_state(path: Any) -> Dict[str, Any]:
    """读取 ``state.json``；不存在/损坏/结构不符均按空 state 处理（WARNING）。"""
    logger = logging.getLogger("engine")
    state_path = Path(path)
    try:
        text = state_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_state()
    except OSError as exc:
        logger.warning("读取 state 失败，按空 state 处理：%s", exc)
        return _empty_state()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.warning("state 文件损坏（%s），按空 state 处理", exc)
        return _empty_state()
    if not isinstance(payload, dict) or not isinstance(payload.get("threads"), dict):
        logger.warning("state 结构不符，按空 state 处理")
        return _empty_state()
    payload.setdefault("version", STATE_VERSION)
    return payload


def save_state(path: Any, state: Dict[str, Any]) -> None:
    """原子写 ``state.json``（同目录 tmp + fsync + os.replace，Windows 重试）。"""
    state_path = Path(path)
    payload = json.dumps(state, ensure_ascii=False, indent=1)
    tmp_path = state_path.with_name(state_path.name + ".tmp")
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise StateError(f"写入 state 临时文件失败：{exc}") from exc
    for attempt in range(3):
        try:
            os.replace(tmp_path, state_path)
            return
        except OSError as exc:
            if attempt >= 2:
                raise StateError(f"替换 state 文件失败：{exc}") from exc
            time.sleep(0.2)


def thread_state(state: Dict[str, Any], tid: int) -> Dict[str, Any]:
    """取（必要时创建）单帖状态并做最小清洗。"""
    threads = state.setdefault("threads", {})
    key = str(tid)
    entry = threads.get(key)
    if not isinstance(entry, dict):
        entry = {}
        threads[key] = entry
    last_page = entry.get("last_page", 0)
    if not isinstance(last_page, int):
        try:
            last_page = int(last_page)
        except (TypeError, ValueError):
            last_page = 0
    entry["last_page"] = max(0, last_page)
    failed = entry.get("failed_pages")
    if not isinstance(failed, list):
        failed = []
    cleaned: List[int] = []
    for value in failed:
        try:
            page = int(value)
        except (TypeError, ValueError):
            continue
        if page > 0 and page not in cleaned:
            cleaned.append(page)
    cleaned.sort()
    entry["failed_pages"] = cleaned
    entry.setdefault("updated_at", None)
    return entry


def select_retry_pages(state: Dict[str, Any], tid: int) -> List[int]:
    """预留：``crawl --retry-failed`` 可直接使用的最早失败页列表。"""
    entry = state.get("threads", {}).get(str(tid), {})
    failed = entry.get("failed_pages") if isinstance(entry, dict) else None
    if not isinstance(failed, list):
        return []
    return sorted({int(page) for page in failed if isinstance(page, int) and page > 0})


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _touch_state(entry: Dict[str, Any]) -> None:
    entry["updated_at"] = _now_iso()


def _mark_success(entry: Dict[str, Any], page: int) -> bool:
    changed = False
    if page > int(entry.get("last_page") or 0):
        entry["last_page"] = page
        changed = True
    failed = entry.setdefault("failed_pages", [])
    if page in failed:
        failed.remove(page)
        changed = True
    return changed


def _mark_failed(entry: Dict[str, Any], page: int) -> bool:
    failed = entry.setdefault("failed_pages", [])
    if page in failed:
        return False
    failed.append(page)
    failed.sort()
    return True


def _safe_save(logger: logging.Logger, state_path: Path, state: Dict[str, Any]) -> bool:
    try:
        save_state(state_path, state)
        return True
    except StateError:
        logger.error("state 保存失败，进度无法持久化，终止本次运行", exc_info=True)
        return False


# ---------------------------------------------------------------------------
# crawl
# ---------------------------------------------------------------------------


def run_crawl(args: Any) -> int:
    """crawl 子命令编排：配置 → 日志 → 目标 → 凭据 → 循环。"""
    cfg = runtime_config.resolve_runtime(args)
    setup_logging(cfg.log_level, cfg.log_path)
    targets_cfg = targets_config.resolve_targets(args)
    state_path = runtime_config.STATE_PATH
    auth = auth_config.load_auth()
    return crawl_loop(
        args,
        cfg=cfg,
        targets_cfg=targets_cfg,
        auth=auth,
        state_path=state_path,
    )


def _fetch_with_429_retry(
    tid: int,
    page: int,
    cfg: runtime_config.RuntimeConfig,
    logger: logging.Logger,
) -> Dict[str, Any]:
    """429 时退避后重试一次（新 ``req_id``）；第二次仍失败由外层处理。"""
    req_id = new_req_id(tid, page)
    try:
        with request_context(req_id):
            return crawler.fetch_page(tid, page, cfg=cfg, req_id=req_id)
    except crawler.RateLimitError as exc:
        wait = max(0.0, float(cfg.backoff_429_seconds)) + random.uniform(0.0, 3.0)
        logger.warning("限流 page=%d（%s），退避 %.1fs 后重试一次", page, exc, wait)
        time.sleep(wait)
        retry_id = new_req_id(tid, page)
        with request_context(retry_id):
            return crawler.fetch_page(tid, page, cfg=cfg, req_id=retry_id)


def crawl_loop(
    args: Any,
    *,
    cfg: runtime_config.RuntimeConfig,
    targets_cfg: targets_config.TargetConfig,
    auth: Optional[auth_config.Auth] = None,
    state_path: Optional[Path] = None,
) -> int:
    """采集主循环：缓存跳过、请求、重试、失败隔离、状态持久化。"""
    logger = logging.getLogger("engine")
    tid = targets_cfg.tid
    state_path = Path(state_path) if state_path is not None else runtime_config.STATE_PATH
    force = bool(getattr(args, "force", False))
    pages_spec = getattr(args, "pages", None)
    if isinstance(pages_spec, str) and not pages_spec.strip():
        pages_spec = None

    state = load_state(state_path)
    entry = thread_state(state, tid)
    last_page = int(entry.get("last_page") or 0)
    failed_pages = list(entry.get("failed_pages") or [])

    page_range = resolve_page_range(
        pages_spec,
        max_pages=targets_cfg.max_pages,
        failed_pages=failed_pages,
        last_page=last_page,
        force=force,
    )
    start = page_range.start
    end = page_range.end

    if end is None and not force:
        cached = crawler.load_cached_page(tid, start, cfg.cache_dir)
        if cached is not None and crawler.is_usable_cache(cached, tid, start):
            end = int(cached["total_pages"])
            logger.info("使用缓存确定总页数 page=%d total=%d", start, end)

    logger.info(
        "启动 crawl tid=%d 区间=%s delay=%.1fs cache_dir=%s parser=%s fetcher=%s auth=%s",
        tid,
        _range_text(start, end),
        cfg.delay_seconds,
        cfg.cache_dir,
        cfg.parser_version,
        cfg.fetcher_version,
        auth.source if auth is not None else "-",
    )
    if targets_cfg.fid or targets_cfg.min_replies:
        logger.info(
            "fid=%d min_replies=%d 已记录；MVP 暂不影响 read.php（列表页为后续项）",
            targets_cfg.fid,
            targets_cfg.min_replies,
        )

    if end is not None and start > end:
        logger.warning("无可抓取页：起点 %d 大于总页数 %d", start, end)
        _touch_state(entry)
        _safe_save(logger, state_path, state)
        return 0

    started_at = time.monotonic()
    success_pages: List[int] = []
    skipped_pages: List[int] = []
    failed_this_run: List[int] = []
    failed_any = False
    consecutive_429 = 0
    first_request = True
    page = start
    current_page = start

    try:
        while end is None or page <= end:
            current_page = page
            # 缓存命中：跳过网络，但仍更新 state
            if not force:
                record = crawler.load_cached_page(tid, page, cfg.cache_dir)
                if record is not None and crawler.is_usable_cache(record, tid, page):
                    changed = _mark_success(entry, page)
                    if end is None:
                        end = int(record["total_pages"])
                    if changed:
                        _touch_state(entry)
                        if not _safe_save(logger, state_path, state):
                            return 2
                    skipped_pages.append(page)
                    success_pages.append(page)
                    consecutive_429 = 0
                    logger.debug("缓存命中跳过 page=%d req_id=%s", page, record.get("req_id"))
                    page += 1
                    continue

            if not first_request:
                time.sleep(cfg.delay_seconds)
            first_request = False

            try:
                record = _fetch_with_429_retry(tid, page, cfg, logger)
                total = int(record["total_pages"])
                _mark_success(entry, page)
                if end is None:
                    end = total
                    logger.info("总页数确定为 %d", total)
                elif end > total:
                    logger.warning("总页数 %d 小于请求终点 %d，已截断", total, end)
                    end = total
                _touch_state(entry)
                if not _safe_save(logger, state_path, state):
                    return 2
                success_pages.append(page)
                consecutive_429 = 0
                logger.info("成功 page=%d total=%d", page, total)
            except crawler.RateLimitError as exc:
                _mark_failed(entry, page)
                failed_this_run.append(page)
                failed_any = True
                consecutive_429 += 1
                _touch_state(entry)
                if not _safe_save(logger, state_path, state):
                    return 2
                logger.error("限流重试仍失败 page=%d：%s", page, exc)
                if end is None:
                    logger.error("首页失败且无法确定终点，本次中止")
                    return 2
                if consecutive_429 >= cfg.max_consecutive_429:
                    logger.error("连续 %d 页限流，中止本次运行", consecutive_429)
                    return 2
            except crawler.AuthError as exc:
                _mark_failed(entry, page)
                failed_this_run.append(page)
                _touch_state(entry)
                _safe_save(logger, state_path, state)
                logger.error(
                    "Cookie 失效，请更新 .env 中的 NGA_COOKIE（或 NGA_UID/NGA_CID）后重试：%s",
                    exc,
                )
                return 2
            except crawler.PageClampedError as exc:
                logger.warning(
                    "请求页 %d 被夹回第 %d 页（总页数 %d），停止本次循环",
                    exc.requested,
                    exc.current,
                    exc.total,
                )
                break
            except crawler.PageError as exc:
                _mark_failed(entry, page)
                failed_this_run.append(page)
                failed_any = True
                consecutive_429 = 0
                _touch_state(entry)
                if not _safe_save(logger, state_path, state):
                    return 2
                if exc.fatal:
                    logger.error("永久错误 page=%d status=%s：%s", page, exc.status, exc)
                    return 2
                if end is None:
                    logger.error("首页失败且无法确定终点，本次中止：%s", exc)
                    return 2
                logger.error("单页失败 page=%d：%s，继续下一页", page, exc)
            except (crawler.CrawlError, crawler.CacheWriteError) as exc:
                _mark_failed(entry, page)
                failed_this_run.append(page)
                failed_any = True
                consecutive_429 = 0
                _touch_state(entry)
                if not _safe_save(logger, state_path, state):
                    return 2
                if end is None:
                    logger.error("首页失败且无法确定终点，本次中止：%s", exc)
                    return 2
                logger.error("单页失败 page=%d：%s，继续下一页", page, exc)

            page += 1
    except KeyboardInterrupt:
        _mark_failed(entry, current_page)
        failed_this_run.append(current_page)
        _touch_state(entry)
        _safe_save(logger, state_path, state)
        logger.warning("用户中断；state 已保存，当前页 %d 可重试", current_page)
        return 130

    _touch_state(entry)
    if not _safe_save(logger, state_path, state):
        return 2

    elapsed = time.monotonic() - started_at
    logger.info(
        "crawl 完成：成功 %d 页（缓存跳过 %d）、失败 %s、耗时 %.1fs、state=%s",
        len(success_pages),
        len(skipped_pages),
        failed_this_run or "无",
        elapsed,
        state_path,
    )
    if failed_any or entry.get("failed_pages"):
        return 2
    return 0


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------


def run_parse(args: Any) -> int:
    """parse 子命令编排：不读凭据、不触网。"""
    cfg = runtime_config.resolve_runtime(args)
    setup_logging(cfg.log_level, cfg.log_path)
    targets_cfg = targets_config.resolve_targets(args)
    return parse_loop(args, cfg=cfg, targets_cfg=targets_cfg)


def _cache_page_path(cache_dir: Any, tid: int, page: int) -> Path:
    return Path(cache_dir) / str(tid) / f"page_{page:03d}.json"


def _scan_cache_pages(cache_dir: Any, tid: int) -> Dict[int, Path]:
    directory = Path(cache_dir) / str(tid)
    if not directory.is_dir():
        return {}
    pages: Dict[int, Path] = {}
    for path in sorted(directory.glob("page_*.json")):
        match = re.fullmatch(r"page_(\d+)\.json", path.name)
        if match is None:
            continue
        number = int(match.group(1))
        if number > 0:
            pages[number] = path
    return pages


def _range_text(start: int, end: Optional[int]) -> str:
    if end is None:
        return f"{start}-"
    if start == end:
        return str(start)
    return f"{start}-{end}"


def _format_fetched_range(values: Sequence[str]) -> Tuple[Optional[str], Optional[str]]:
    moments: List[datetime] = []
    for value in values:
        if not value:
            continue
        try:
            moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            continue
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moments.append(moment)
    if not moments:
        return None, None
    earliest = min(moments).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    latest = max(moments).astimezone().strftime("%Y-%m-%d %H:%M:%S")
    return earliest, latest


def _dedupe_key(post: Any, page: int, index: int) -> Tuple[Any, ...]:
    pid = getattr(post, "pid", None)
    if pid is not None:
        return ("pid", pid)
    lou = getattr(post, "lou", None)
    if lou is not None:
        return ("page-lou", page, lou)
    return ("page-index", page, index)


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logging.getLogger("engine").warning("清理临时文件失败：%s", path)


class _PostWriter:
    """把渲染后的楼层写入句柄；块之间恰好一个空行。"""

    def __init__(self, handle: Any) -> None:
        self.handle = handle
        self.first = True
        self.count = 0

    def write(self, text: str) -> None:
        self.handle.write("" if self.first else "\n\n")
        self.handle.write(text)
        self.first = False
        self.count += 1


def _collect_page_meta(
    parsed: Any,
    stats: Dict[str, Any],
    logger: logging.Logger,
    page: int,
) -> None:
    if parsed.fetched_at:
        stats["fetched"].append(parsed.fetched_at)
    if parsed.fetcher_version and parsed.fetcher_version not in stats["fetcher_versions"]:
        stats["fetcher_versions"].append(parsed.fetcher_version)
    if stats["total_pages"] is None and isinstance(parsed.total_pages, int):
        stats["total_pages"] = parsed.total_pages
    if stats["title"] is None and parsed.title:
        stats["title"] = parsed.title
    if stats["op_author"] is None and parsed.op_author:
        stats["op_author"] = parsed.op_author


def _process_pages(
    pages: Sequence[int],
    cache_files: Dict[int, Path],
    *,
    tid: int,
    cfg: runtime_config.RuntimeConfig,
    writer: _PostWriter,
    seen: Set[Tuple[Any, ...]],
    logger: logging.Logger,
) -> Dict[str, Any]:
    """逐页解析并写入；单页失败只告警，不中断整体。"""
    stats: Dict[str, Any] = {
        "thread_pages": 0,
        "skipped": 0,
        "parse_errors": 0,
        "missing": 0,
        "title": None,
        "op_author": None,
        "total_pages": None,
        "fetcher_versions": [],
        "fetched": [],
    }
    for page in pages:
        if page not in cache_files:
            logger.warning("缺少缓存页 page=%d，跳过（可重跑 crawl 补齐）", page)
            stats["skipped"] += 1
            continue
        record = crawler.load_cached_page(tid, page, cfg.cache_dir)
        if record is None:
            logger.warning("缓存页损坏或不可读 page=%d，跳过", page)
            stats["skipped"] += 1
            continue
        try:
            parsed = parser_module.parse_cache(record)
        except Exception:  # 解析器应按契约不抛异常；这里仅防御
            logger.warning("解析异常 page=%d，跳过", page, exc_info=True)
            stats["parse_errors"] += 1
            continue
        for warning in parsed.warnings:
            logger.warning("page=%d %s", page, warning)
        if parsed.skipped:
            logger.warning(
                "page=%d 非 thread 页面（status=%s page_type=%s），跳过：%s",
                page,
                parsed.status,
                parsed.page_type,
                parsed.skip_reason,
            )
            stats["skipped"] += 1
            continue
        _collect_page_meta(parsed, stats, logger, page)
        stats["thread_pages"] += 1
        page_missing = sum(parsed.missing_fields.values()) if parsed.missing_fields else 0
        logger.info(
            "解析 page=%d posts=%d missing=%d",
            page,
            len(parsed.posts),
            page_missing,
        )
        for index, post in enumerate(parsed.posts):
            key = _dedupe_key(post, page, index)
            if key in seen:
                logger.warning("重复帖子跳过 page=%d pid=%s lou=%s", page, post.pid, post.lou)
                continue
            seen.add(key)
            writer.write(render.render_post(post))
            stats["missing"] += len(post.missing_fields)
    return stats


def parse_loop(
    args: Any,
    *,
    cfg: runtime_config.RuntimeConfig,
    targets_cfg: targets_config.TargetConfig,
    output_dir: Optional[Path] = None,
) -> int:
    """离线解析循环：扫描缓存 → 过滤区间 → 解析渲染 → 原子输出 TXT。"""
    logger = logging.getLogger("engine")
    tid = targets_cfg.tid
    output_dir = Path(output_dir) if output_dir is not None else runtime_config.OUTPUT_DIR
    output_path = output_dir / f"{tid}.txt"
    started_at = time.monotonic()

    cache_files = _scan_cache_pages(cfg.cache_dir, tid)
    if not cache_files:
        logger.error(
            "缓存目录中没有可解析页面：%s\n"
            "请先运行 python main.py crawl --tid %d 抓取该帖子，或检查 --cache-dir 是否正确",
            Path(cfg.cache_dir) / str(tid),
            tid,
        )
        return 2
    max_page = max(cache_files)

    spec = getattr(args, "pages", None)
    if isinstance(spec, str) and not spec.strip():
        spec = None
    try:
        if spec is None or str(spec).strip().lower() == "all":
            start, end = 1, max_page
        else:
            page_range = parse_page_range(spec)
            start = page_range.start
            end = page_range.end if page_range.end is not None else max_page
    except ValueError:
        raise

    if start > end:
        logger.error(
            "解析区间为空（%s），没有可输出内容；最大缓存页为 %d，"
            "请用 --pages 指定有效范围或先 crawl 补齐缓存",
            _range_text(start, end),
            max_page,
        )
        return 2

    force = bool(getattr(args, "force", False))
    rebuild = force or (not output_path.exists()) or _file_size(output_path) == 0
    logger.info(
        "启动 parse tid=%d 区间=%s cache_dir=%s 输出=%s 模式=%s",
        tid,
        _range_text(start, end),
        cfg.cache_dir,
        output_path,
        "重建" if rebuild else "追加",
    )

    pages = list(range(start, end + 1))
    seen: Set[Tuple[Any, ...]] = set()
    body_tmp = output_path.with_name(output_path.name + ".body.tmp")
    final_tmp = output_path.with_name(output_path.name + ".tmp")
    # 启动时若发现残留 tmp，不自动清理，仅告警（可能来自上次异常中断）
    for leftover in (body_tmp, final_tmp):
        if leftover.exists():
            logger.warning("发现残留临时文件，本次将覆盖：%s", leftover)

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        if not rebuild and _file_size(output_path) == 0:
            # 输出文件在判断之后被清空/删除：退回“重建”语义，避免追加到空文件。
            _safe_unlink(output_path)
            rebuild = True
        if rebuild:
            with open(body_tmp, "w", encoding="utf-8", newline="\n") as body_handle:
                writer = _PostWriter(body_handle)
                stats = _process_pages(
                    pages,
                    cache_files,
                    tid=tid,
                    cfg=cfg,
                    writer=writer,
                    seen=seen,
                    logger=logger,
                )
            if stats["thread_pages"] == 0 or writer.count == 0:
                logger.error(
                    "没有可输出的 thread 页面（区间 %s）：缓存可能只有登录墙/错误页，"
                    "请检查 Cookie 后重新 crawl，或核对 --cache-dir",
                    _range_text(start, end),
                )
                _safe_unlink(body_tmp)
                return 2
            fetched_min, fetched_max = _format_fetched_range(stats["fetched"])
            meta = render.OutputMeta(
                tid=tid,
                title=stats["title"],
                op_author=stats["op_author"],
                total_pages=stats["total_pages"]
                if stats["total_pages"] is not None
                else max_page,
                page_range=_range_text(start, end),
                fetched_min=fetched_min,
                fetched_max=fetched_max,
                parser_version=cfg.parser_version,
                fetcher_version=", ".join(stats["fetcher_versions"]) or None,
                post_count=writer.count,
                missing_count=stats["missing"],
            )
            with open(final_tmp, "w", encoding="utf-8", newline="\n") as final_handle:
                final_handle.write(render.render_header(meta))
                final_handle.write("\n\n")
                with open(body_tmp, "r", encoding="utf-8") as body_reader:
                    shutil.copyfileobj(body_reader, final_handle)
                final_handle.write("\n")
            os.replace(final_tmp, output_path)
            _safe_unlink(body_tmp)
            mode_text = "重建"
        else:
            with open(output_path, "a", encoding="utf-8", newline="\n") as append_handle:
                writer = _PostWriter(append_handle)
                stats = _process_pages(
                    pages,
                    cache_files,
                    tid=tid,
                    cfg=cfg,
                    writer=writer,
                    seen=seen,
                    logger=logger,
                )
            if stats["thread_pages"] == 0 or writer.count == 0:
                logger.error(
                    "没有可输出的 thread 页面（区间 %s）：缓存可能只有登录墙/错误页，"
                    "请检查 Cookie 后重新 crawl，或核对 --cache-dir",
                    _range_text(start, end),
                )
                return 2
            mode_text = "追加"
    except KeyboardInterrupt:
        _safe_unlink(body_tmp)
        _safe_unlink(final_tmp)
        logger.warning("用户中断；已清理临时文件，旧输出保持不动")
        return 130
    except OSError as exc:
        _safe_unlink(body_tmp)
        _safe_unlink(final_tmp)
        logger.error("写入输出失败：%s（旧输出保持不变）", exc)
        return 2
    except Exception:
        _safe_unlink(body_tmp)
        _safe_unlink(final_tmp)
        raise

    elapsed = time.monotonic() - started_at
    logger.info(
        "parse 完成：解析 %d 页、跳过 %d 页、帖子 %d、字段缺失 %d 处、耗时 %.1fs、输出=%s（%s）",
        stats["thread_pages"],
        stats["skipped"],
        writer.count,
        stats["missing"],
        elapsed,
        output_path,
        mode_text,
    )
    return 0


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0
