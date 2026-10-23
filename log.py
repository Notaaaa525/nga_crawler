"""日志初始化、轮转、``req_id`` 注入与脱敏。

纪律：日志绝不记录 Cookie、``Set-Cookie``、完整请求头或完整响应体。
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import logging.handlers
import uuid
from pathlib import Path
from typing import Iterator, Optional, Union

LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] [%(req_id)s] %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

_HANDLER_FLAG = "_nga_crawler_handler"

_req_id_var: contextvars.ContextVar = contextvars.ContextVar("nga_req_id", default="-")


class LogSetupError(RuntimeError):
    """日志目录/文件无法创建。"""


class _ReqIdFilter(logging.Filter):
    """把当前上下文的 ``req_id`` 注入 LogRecord；无上下文时为 ``-``。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.req_id = _req_id_var.get()
        return True


_REQ_ID_FILTER = _ReqIdFilter()


def new_req_id(tid: int, page: int) -> str:
    """生成单页请求追踪号：``{tid}-{page}-{uuid8}``。"""
    return f"{tid}-{page}-{uuid.uuid4().hex[:8]}"


def current_req_id() -> str:
    return _req_id_var.get()


@contextlib.contextmanager
def request_context(req_id: Optional[str]) -> Iterator[str]:
    """在上下文中绑定 ``req_id``，退出时恢复。"""
    token = _req_id_var.set(req_id or "-")
    try:
        yield req_id or "-"
    finally:
        _req_id_var.reset(token)


def mask_secret(value: Optional[str], keep: int = 4) -> str:
    """脱敏：保留前 ``keep`` 位，其余用 ``***`` 代替。"""
    if not value:
        return "***"
    text = str(value)
    if len(text) <= keep:
        return text[:1] + "***"
    return text[:keep] + "***"


def _drop_our_handlers(root: logging.Logger) -> None:
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_FLAG, False):
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:  # pragma: no cover - 关闭失败不影响重装
                pass
    for existing in list(root.filters):
        if isinstance(existing, _ReqIdFilter):
            root.removeFilter(existing)


def _build_console_handler(level: int) -> logging.Handler:
    handler = logging.StreamHandler()
    handler.setLevel(max(level, logging.INFO))
    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    handler.addFilter(_REQ_ID_FILTER)
    setattr(handler, _HANDLER_FLAG, True)
    try:  # Windows 控制台编码偶发不支持字符；尽量用 replace 兜底
        stream = handler.stream
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(errors="replace")
    except Exception:  # pragma: no cover - 仅防御
        pass
    return handler


def setup_logging(level: str = "INFO", log_path: Optional[Union[str, Path]] = None) -> None:
    """初始化根 logger：轮转文件 + 控制台；重复调用幂等。

    文件 handler 使用配置级别，控制台使用 ``max(配置级别, INFO)``，
    避免 DEBUG 刷屏。轮转参数写死为 5MB × 3。
    """
    level_name = str(level).upper()
    numeric = getattr(logging, level_name, None)
    if not isinstance(numeric, int):
        raise LogSetupError(f"非法日志级别：{level!r}")
    path = Path(log_path) if log_path is not None else Path("logs/nga.log")

    root = logging.getLogger()
    _drop_our_handlers(root)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            str(path),
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
    except OSError as exc:
        raise LogSetupError(f"无法创建日志文件 {path}：{exc}") from exc

    file_handler.setLevel(numeric)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    file_handler.addFilter(_REQ_ID_FILTER)
    setattr(file_handler, _HANDLER_FLAG, True)

    root.setLevel(numeric)
    root.addFilter(_REQ_ID_FILTER)
    root.addHandler(file_handler)
    root.addHandler(_build_console_handler(numeric))
