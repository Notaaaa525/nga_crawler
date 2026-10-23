"""命令行入口：argparse 子命令、参数翻译与退出码映射。

人机交互约定（见 ``docs/cli-ux-improvements.md``）：

- ``-h`` 首屏即可自助：示例、凭据配置、退出码、子命令入口一次说清；
- 帮助分节标题与 ``-h`` 文案本地化，不再中英混排；
- 每个参数在帮助里写明“生效默认值 + 可用环境变量”；
- 中文帮助按终端显示宽度折行，避免帮助文本超出窗口；
- 参数错误除回显用法外，额外提示下一步命令；
- 无子命令时把帮助与提示写到 stderr，stdout 保持干净（便于管道）；
- ``-V/--version`` 显示版本。

纪律：cli 不 import 网络库，不读写缓存/输出/state；只把 ``Namespace`` 交给 engine。
"""
from __future__ import annotations

import argparse
import logging
import sys
import unicodedata
from typing import List, Optional, Sequence

import engine
import log as log_module
from config import auth as auth_config
from config import runtime as runtime_config
from config import targets as targets_config

VERSION_TEXT = (
    f"NGA 特化爬虫 {runtime_config.PARSER_VERSION}"
    f"（parser {runtime_config.PARSER_VERSION} / fetcher {runtime_config.FETCHER_VERSION}）"
)

_TOP_EPILOG = """\
示例:
  # 首次使用：把 .env.example 复制为 .env，填入凭据（二选一）
  #   NGA_COOKIE=ngaPassportUid=...; ngaPassportCid=...
  #   或 NGA_UID=... 与 NGA_CID=...
  # 也可直接设置同名环境变量（优先于 .env）
  python main.py crawl --tid 47455536 --pages 1-20
  python main.py crawl --tid 47455536            # 省略 --pages = 默认续跑
  python main.py parse --tid 47455536 --force

提示:
  * 子命令参数与更多示例：python main.py crawl -h / python main.py parse -h
  * 运行参数优先级：CLI > 环境变量 > 默认值；凭据：环境变量 > .env
  * 退出码：0 成功 / 1 参数错误 / 2 运行时错误 / 130 用户中断
"""

_CRAWL_EPILOG = """\
示例:
  python main.py crawl --tid 47455536 --pages 1-20
  python main.py crawl --tid 47455536 --pages 21-
  python main.py crawl --tid 47455536 --force

提示:
  * 省略 --pages 时默认续跑：从最早失败页或 last_page+1 开始
  * 首次使用请先配置 .env（参考 .env.example）；Cookie 失效会立即退出 2
  * 每页请求后按 --delay 限速；遇到 429 会退避后重试一次
"""

_PARSE_EPILOG = """\
示例:
  python main.py parse --tid 47455536 --format txt --force
  python main.py parse --tid 47455536 --pages 1-10

提示:
  * parse 只读本地缓存，不联网、不需要 Cookie，可反复重跑
  * 缺失缓存页只告警跳过；推荐用 --force 重建以保证文件头统计最新
"""


class CliUsageError(RuntimeError):
    """CLI 语法/取值非法（映射退出码 1）。"""


def _display_width(text: str) -> int:
    """按终端显示宽度计算字符串宽度（中文等全角字符记 2 列）。"""
    return sum(
        2 if unicodedata.east_asian_width(char) in ("W", "F") else 1 for char in text
    )


def _tokenize_for_wrap(text: str) -> List[str]:
    """切分为“ASCII 词 / 单个非 ASCII 字符 / 单个空格”，供按显示宽度折行。"""
    tokens: List[str] = []
    buffer: List[str] = []
    for char in text:
        if char.isspace():
            if buffer:
                tokens.append("".join(buffer))
                buffer = []
            tokens.append(char)
        elif ord(char) < 128:
            buffer.append(char)
        else:
            if buffer:
                tokens.append("".join(buffer))
                buffer = []
            tokens.append(char)
    if buffer:
        tokens.append("".join(buffer))
    return tokens


_CLOSING_PUNCT = "、。，．；：！？）〕】》」』”’…·%"
_OPENING_PUNCT = "（〔【《「『“‘"


def _fix_line_breaks(lines: List[str]) -> List[str]:
    """中文禁则处理：不让行首出现收尾标点、行尾出现开头标点。"""
    index = 1
    while index < len(lines):
        line = lines[index]
        if line and line[0] in _CLOSING_PUNCT and lines[index - 1]:
            lines[index - 1] += line[0]
            line = line[1:].lstrip()
            lines[index] = line
            if not line:
                del lines[index]
                continue
        index += 1
    index = 0
    while index < len(lines) - 1:
        line = lines[index]
        moved = ""
        while line and line[-1] in _OPENING_PUNCT:
            moved = line[-1] + moved
            line = line[:-1]
        if moved:
            lines[index] = line.rstrip()
            lines[index + 1] = moved + lines[index + 1]
            if not lines[index]:
                del lines[index]
                continue
        index += 1
    return lines


def _wrap_display_width(text: str, width: int) -> List[str]:
    """按显示宽度折行：中文不再按“字符数”误判导致帮助文本超出终端。"""
    if width <= 0:
        return [text]
    lines: List[str] = []
    for raw_line in text.splitlines() or [""]:
        if not raw_line:
            lines.append("")
            continue
        current = ""
        current_width = 0
        for token in _tokenize_for_wrap(raw_line):
            token_width = _display_width(token)
            if token.isspace():
                if current and not current.endswith(" "):
                    current += " "
                    current_width += 1
                continue
            if current and current_width + token_width > width:
                lines.append(current.rstrip())
                current = token
                current_width = token_width
            else:
                current += token
                current_width += token_width
        if current or not lines:
            lines.append(current.rstrip())
    return _fix_line_breaks(lines)


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """中文帮助按显示宽度折行；描述与 epilog 保留手工排版。"""

    def _split_lines(self, text: str, width: int) -> List[str]:
        return _wrap_display_width(text, width)


class _ArgumentParser(argparse.ArgumentParser):
    """本地化帮助，并把 argparse 的 ``SystemExit(2)`` 改为可捕获异常。"""

    def error(self, message: str):  # type: ignore[override]
        raise CliUsageError(message)

    def format_usage(self) -> str:
        return super().format_usage().replace("usage: ", "用法: ", 1)

    def format_help(self) -> str:
        return super().format_help().replace("usage: ", "用法: ", 1)


def _localize_sections(parser: argparse.ArgumentParser) -> None:
    """把 argparse 默认英文分节标题换成中文（无公开 API，按属性设置）。"""
    for attribute, title in (("_positionals", "位置参数"), ("_optionals", "选项")):
        section = getattr(parser, attribute, None)
        if section is not None and hasattr(section, "title"):
            section.title = title


def _add_help_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-h",
        "--help",
        action="help",
        default=argparse.SUPPRESS,
        help="显示帮助信息并退出",
    )


def _default_tid_text() -> str:
    default = targets_config.DEFAULT_TID
    if isinstance(default, int) and not isinstance(default, bool):
        return f"默认 {default}"
    return "默认未配置"


def _positive_int(text: str) -> int:
    try:
        value = int(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"{text!r} 不是整数") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{text!r} 必须为正整数")
    return value


def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"{text!r} 不是整数") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"{text!r} 不能为负数")
    return value


def _non_negative_float(text: str) -> float:
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"{text!r} 不是数字") from None
    if value < 0:
        raise argparse.ArgumentTypeError(f"{text!r} 不能为负数")
    return value


def _positive_float(text: str) -> float:
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError(f"{text!r} 不是数字") from None
    if value <= 0:
        raise argparse.ArgumentTypeError(f"{text!r} 必须大于 0")
    return value


def _log_level(text: str) -> str:
    level = text.strip().upper()
    if level not in runtime_config.LOG_LEVELS:
        raise argparse.ArgumentTypeError(
            f"{text!r} 非法，可选 {', '.join(runtime_config.LOG_LEVELS)}"
        )
    return level


def _add_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tid",
        type=_positive_int,
        default=None,
        metavar="TID",
        help=f"帖子 ID，必须为正整数（{_default_tid_text()}，见 config/targets.py）",
    )
    parser.add_argument(
        "--cache-dir",
        default=None,
        metavar="DIR",
        help="缓存根目录（默认 cache，相对 nga_crawler/；环境变量 NGA_CACHE_DIR）",
    )
    parser.add_argument(
        "--log-level",
        type=_log_level,
        default=None,
        metavar="LEVEL",
        help="日志级别 DEBUG/INFO/WARNING/ERROR/CRITICAL（默认 INFO；环境变量 NGA_LOG_LEVEL）",
    )
    parser.add_argument(
        "--log-path",
        default=None,
        metavar="FILE",
        help="日志文件（默认 logs/nga.log，相对 nga_crawler/；环境变量 NGA_LOG_PATH）",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="main.py",
        description="NGA 特化爬虫：crawl 采集帖子页缓存，parse 离线导出 TXT。",
        epilog=_TOP_EPILOG,
        formatter_class=_HelpFormatter,
        add_help=False,
    )
    _localize_sections(parser)
    _add_help_option(parser)
    parser.add_argument(
        "-V",
        "--version",
        action="version",
        version=VERSION_TEXT,
        help="显示版本信息并退出",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<命令>")

    crawl = subparsers.add_parser(
        "crawl",
        help="抓取帖子页并写入缓存（需要 Cookie）",
        description="逐页抓取帖子、按页写入缓存，并根据 state 支持断点续跑。",
        epilog=_CRAWL_EPILOG,
        formatter_class=_HelpFormatter,
        add_help=False,
    )
    _localize_sections(crawl)
    _add_help_option(crawl)
    _add_common_options(crawl)
    crawl.add_argument(
        "--pages",
        default=None,
        metavar="SPEC",
        help=(
            "页码区间：all / N / A-B / A-（省略=默认续跑：从最早失败页或 last_page+1 开始；"
            "指定 --force 时等价 all）"
        ),
    )
    crawl.add_argument(
        "--max-pages",
        type=_non_negative_int,
        default=None,
        metavar="N",
        help="本次最多抓多少页，0 表示不限制（默认 0）",
    )
    crawl.add_argument(
        "--fid",
        type=_non_negative_int,
        default=None,
        metavar="FID",
        help="板块 ID（列表页预留，MVP 不影响 read.php；默认 0）",
    )
    crawl.add_argument(
        "--min-replies",
        type=_non_negative_int,
        default=None,
        metavar="N",
        help="最少回复数（列表页预留，MVP 不影响 read.php；默认 0）",
    )
    crawl.add_argument(
        "--force",
        action="store_true",
        help="忽略缓存重抓；不重置 state；省略 --pages 时等价 all",
    )
    crawl.add_argument(
        "--delay",
        type=_non_negative_float,
        default=None,
        metavar="SEC",
        help="页间间隔秒数，需 >=0（默认 2.0；环境变量 NGA_DELAY_SECONDS）",
    )
    crawl.add_argument(
        "--retries",
        type=_non_negative_int,
        default=None,
        metavar="N",
        help="网络/5xx 重试次数，需 >=0（默认 3；环境变量 NGA_MAX_RETRIES）",
    )
    crawl.add_argument(
        "--backoff-429",
        dest="backoff_429",
        type=_non_negative_float,
        default=None,
        metavar="SEC",
        help="429 退避基数秒数，另加 0-3s 抖动（默认 15；环境变量 NGA_BACKOFF_429_SECONDS）",
    )
    crawl.add_argument(
        "--timeout",
        type=_positive_float,
        default=None,
        metavar="SEC",
        help="单次请求超时秒数，需 >0（默认 30；环境变量 NGA_TIMEOUT）",
    )

    parse = subparsers.add_parser(
        "parse",
        help="离线解析缓存并输出 TXT（不需要 Cookie）",
        description="读取本地缓存，解析楼层并生成 TXT；不触网、不读取凭据。",
        epilog=_PARSE_EPILOG,
        formatter_class=_HelpFormatter,
        add_help=False,
    )
    _localize_sections(parse)
    _add_help_option(parse)
    _add_common_options(parse)
    parse.add_argument(
        "--pages",
        default=None,
        metavar="SPEC",
        help="解析区间：all / N / A-B / A-（默认 all；开区间终点取最大缓存页）",
    )
    parse.add_argument(
        "--format",
        choices=["txt"],
        default="txt",
        help="输出格式（MVP 仅 txt；json 等格式尚未实现）",
    )
    parse.add_argument(
        "--force",
        action="store_true",
        help="清空并重建输出文件（推荐，保证文件头与统计最新）",
    )
    return parser


def _validate_arguments(args: argparse.Namespace) -> None:
    """非 argparse 能覆盖的取值校验；失败统一映射退出码 1。"""
    try:
        targets_config.resolve_targets(args)
    except ValueError as exc:
        raise CliUsageError(str(exc)) from None

    pages = getattr(args, "pages", None)
    if isinstance(pages, str) and pages.strip():
        try:
            engine.parse_page_range(pages)
        except ValueError as exc:
            raise CliUsageError(f"--pages 非法：{exc}") from None

    output_format = getattr(args, "format", "txt")
    if output_format != "txt":
        raise CliUsageError(f"--format 目前只支持 txt，收到 {output_format!r}")


def _dispatch(args: argparse.Namespace) -> int:
    _validate_arguments(args)
    if args.command == "crawl":
        return engine.run_crawl(args)
    return engine.run_parse(args)


def _select_usage_parser(
    parser: argparse.ArgumentParser, command: Optional[str]
) -> argparse.ArgumentParser:
    """参数错误时优先回显对应子命令的用法（拿不到则退回顶层）。"""
    if not command:
        return parser
    for action in getattr(parser, "_actions", []):
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict) and command in choices:
            candidate = choices[command]
            if isinstance(candidate, argparse.ArgumentParser):
                return candidate
    return parser


def _print_usage_error(
    parser: argparse.ArgumentParser,
    message: str,
    command: Optional[str] = None,
) -> None:
    """参数错误：回显原因 + 用法 + 下一步提示（全部写 stderr）。"""
    print(f"参数错误: {message}", file=sys.stderr)
    _select_usage_parser(parser, command).print_usage(sys.stderr)
    hint = f"python main.py {command} -h" if command else "python main.py -h"
    print(f"提示：使用 {hint} 查看可用参数与示例", file=sys.stderr)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """解析参数并分发；返回进程退出码（0/1/2/130）。"""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except CliUsageError as exc:
        _print_usage_error(parser, str(exc))
        return 1

    command = getattr(args, "command", None)
    if command is None:
        # 缺少子命令属于用法错误：帮助与提示写 stderr，stdout 保持干净。
        parser.print_help(sys.stderr)
        print("", file=sys.stderr)
        print(
            "提示：请指定子命令 crawl 或 parse；"
            "例如 python main.py crawl --tid <tid> --pages 1-20",
            file=sys.stderr,
        )
        return 1

    try:
        return _dispatch(args)
    except CliUsageError as exc:
        _print_usage_error(parser, str(exc), command=command)
        return 1
    except (auth_config.AuthConfigError, runtime_config.RuntimeConfigError) as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2
    except log_module.LogSetupError as exc:
        print(f"日志初始化失败: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    except SystemExit:
        raise
    except Exception as exc:  # 兜底：不把堆栈打到 stdout
        logging.getLogger("cli").error("未预期异常", exc_info=True)
        print(f"运行时错误: {exc}", file=sys.stderr)
        return 2
