"""运行参数配置：限速、重试、路径、版本与日志。

加载优先级：``CLI > 环境变量 > 默认值``。

本模块只做配置解析与校验，不触网、不写文件。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

BASE_DIR = Path(__file__).resolve().parents[1]
"""项目根目录（``nga_crawler/``）；所有相对路径都锚定到这里。"""

DEFAULT_DELAY_SECONDS = 2.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_429_SECONDS = 15.0
DEFAULT_MAX_CONSECUTIVE_429 = 3
DEFAULT_TIMEOUT = 30.0
DEFAULT_CACHE_DIR = "cache"
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_LOG_PATH = "logs/nga.log"

PARSER_VERSION = "1.0.0"
FETCHER_VERSION = "1.0.0"

BASE_URL = "https://bbs.nga.cn/read.php"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

STATE_PATH = BASE_DIR / "state.json"
OUTPUT_DIR = BASE_DIR / "output"


class RuntimeConfigError(RuntimeError):
    """运行参数非法（含环境变量取值非法）。"""


@dataclass(frozen=True)
class RuntimeConfig:
    """一次运行生效的全部运行参数。"""

    delay_seconds: float
    max_retries: int
    backoff_429_seconds: float
    max_consecutive_429: int
    timeout: float
    cache_dir: Path
    log_level: str
    log_path: Path
    parser_version: str = PARSER_VERSION
    fetcher_version: str = FETCHER_VERSION
    base_url: str = BASE_URL
    user_agent: str = USER_AGENT


def _clean_env(name: str) -> Optional[str]:
    """读取环境变量；空串/纯空白视为未设置。"""
    raw = os.environ.get(name)
    if raw is None:
        return None
    raw = raw.strip()
    return raw or None


def _get_arg(args: Any, name: str) -> Any:
    if args is None:
        return None
    return getattr(args, name, None)


def _resolve_float(
    cli_value: Any,
    env_name: str,
    default: float,
    *,
    minimum: Optional[float] = None,
    exclusive_min: bool = False,
) -> float:
    if cli_value is not None:
        # CLI 取值已由 argparse 校验；这里只做数值转换。
        return float(cli_value)
    raw = _clean_env(env_name)
    if raw is None:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        raise RuntimeConfigError(f"环境变量 {env_name} 非法：{raw!r}，应为数字") from None
    if minimum is not None:
        if exclusive_min and value <= minimum:
            raise RuntimeConfigError(f"环境变量 {env_name} 非法：{raw!r}，要求大于 {minimum}")
        if not exclusive_min and value < minimum:
            raise RuntimeConfigError(f"环境变量 {env_name} 非法：{raw!r}，要求不小于 {minimum}")
    return value


def _resolve_int(
    cli_value: Any,
    env_name: str,
    default: int,
    *,
    minimum: Optional[int] = None,
) -> int:
    if cli_value is not None:
        return int(cli_value)
    raw = _clean_env(env_name)
    if raw is None:
        return int(default)
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeConfigError(f"环境变量 {env_name} 非法：{raw!r}，应为整数") from None
    if minimum is not None and value < minimum:
        raise RuntimeConfigError(f"环境变量 {env_name} 非法：{raw!r}，要求不小于 {minimum}")
    return value


def _resolve_path(cli_value: Any, env_name: str, default: str) -> Path:
    raw = cli_value
    if raw is None:
        raw = _clean_env(env_name) or default
    path = Path(raw)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def _resolve_log_level(cli_value: Any) -> str:
    raw = cli_value if cli_value is not None else _clean_env("NGA_LOG_LEVEL")
    if raw is None:
        return DEFAULT_LOG_LEVEL
    level = str(raw).strip().upper()
    if level not in LOG_LEVELS:
        raise RuntimeConfigError(
            f"环境变量 NGA_LOG_LEVEL 非法：{raw!r}，可选 {', '.join(LOG_LEVELS)}"
        )
    return level


def resolve_runtime(args: Any = None) -> RuntimeConfig:
    """合并 CLI / 环境变量 / 默认值，返回不可变的运行配置。"""
    return RuntimeConfig(
        delay_seconds=_resolve_float(
            _get_arg(args, "delay"),
            "NGA_DELAY_SECONDS",
            DEFAULT_DELAY_SECONDS,
            minimum=0.0,
        ),
        max_retries=_resolve_int(
            _get_arg(args, "retries"),
            "NGA_MAX_RETRIES",
            DEFAULT_MAX_RETRIES,
            minimum=0,
        ),
        backoff_429_seconds=_resolve_float(
            _get_arg(args, "backoff_429"),
            "NGA_BACKOFF_429_SECONDS",
            DEFAULT_BACKOFF_429_SECONDS,
            minimum=0.0,
        ),
        max_consecutive_429=DEFAULT_MAX_CONSECUTIVE_429,
        timeout=_resolve_float(
            _get_arg(args, "timeout"),
            "NGA_TIMEOUT",
            DEFAULT_TIMEOUT,
            minimum=0.0,
            exclusive_min=True,
        ),
        cache_dir=_resolve_path(_get_arg(args, "cache_dir"), "NGA_CACHE_DIR", DEFAULT_CACHE_DIR),
        log_level=_resolve_log_level(_get_arg(args, "log_level")),
        log_path=_resolve_path(_get_arg(args, "log_path"), "NGA_LOG_PATH", DEFAULT_LOG_PATH),
    )
