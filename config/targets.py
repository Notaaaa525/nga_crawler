"""抓取目标配置：这次抓什么。

加载优先级：``CLI > 默认值``。MVP 只使用 ``tid`` / ``max_pages``；
``fid`` / ``min_replies`` 属于列表页需求，先在配置与 CLI 层预留。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

DEFAULT_TID = 47455536  # 需求示例值，可按需修改
DEFAULT_FID = 0
DEFAULT_MAX_PAGES = 0  # 0 = 不额外限制
DEFAULT_MIN_REPLIES = 0


@dataclass(frozen=True)
class TargetConfig:
    """本次运行的目标配置。"""

    tid: int
    fid: int = DEFAULT_FID
    max_pages: int = DEFAULT_MAX_PAGES
    min_replies: int = DEFAULT_MIN_REPLIES


def _get_arg(args: Any, name: str) -> Any:
    if args is None:
        return None
    return getattr(args, name, None)


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"取值非法，应为整数：{value!r}") from None


def resolve_targets(args: Any = None) -> TargetConfig:
    """合并 CLI 与默认值；非法取值抛 :class:`ValueError`（CLI 映射为退出码 1）。"""
    tid = _get_arg(args, "tid")
    if tid is None:
        tid = DEFAULT_TID
    if tid is None:
        raise ValueError("缺少 --tid，且 config/targets.py 未配置默认 TID")
    tid = _as_int(tid)
    if tid <= 0:
        raise ValueError(f"--tid 必须为正整数，收到 {tid!r}")

    fid = _get_arg(args, "fid")
    fid = DEFAULT_FID if fid is None else _as_int(fid)
    if fid < 0:
        raise ValueError(f"--fid 不能为负数，收到 {fid!r}")

    max_pages = _get_arg(args, "max_pages")
    max_pages = DEFAULT_MAX_PAGES if max_pages is None else _as_int(max_pages)
    if max_pages < 0:
        raise ValueError(f"--max-pages 不能为负数，收到 {max_pages!r}")

    min_replies = _get_arg(args, "min_replies")
    min_replies = DEFAULT_MIN_REPLIES if min_replies is None else _as_int(min_replies)
    if min_replies < 0:
        raise ValueError(f"--min-replies 不能为负数，收到 {min_replies!r}")

    return TargetConfig(tid=tid, fid=fid, max_pages=max_pages, min_replies=min_replies)
