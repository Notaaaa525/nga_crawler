"""NGA 特化爬虫 - 交互菜单（Windows 双击 ``run.bat`` 启动）。

为什么菜单不在 ``run.bat`` 里？
- ``cmd.exe`` 解析 UTF-8 批处理文件时对中文不稳定：多字节字符会让命令行被截断，
  之前出现过菜单行被当作命令执行的错误（``'xx指定页码范围' is not recognized``）。
- 因此 ``run.bat`` 只保留纯 ASCII 的“探测 Python + 启动本文件”逻辑；
  菜单文案与控制流都在 Python 中（源码固定 UTF-8，控制台输出走 Unicode API）。

菜单只调用 ``python main.py ...``，不读取、不写入任何凭据内容。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent
MAIN_PY = BASE_DIR / "main.py"
ENV_FILE = BASE_DIR / ".env"
ENV_EXAMPLE = BASE_DIR / ".env.example"
STATE_FILE = BASE_DIR / "state.json"
OUTPUT_DIR = BASE_DIR / "output"

EXIT_MEANINGS = {
    0: "成功",
    1: "参数错误",
    2: "运行时错误",
    130: "用户中断",
}

_PAGES_RE = re.compile(r"^(?:all|\d+(?:-\d*)?)$", re.IGNORECASE)

MENU_TEXT = """\
============================================================
    NGA 特化爬虫 - 快捷菜单
============================================================
    [1] 抓取整帖（全部页）
    [2] 抓取指定页码范围
    [3] 默认续跑（补抓失败页）
    [4] 强制重抓（忽略缓存）
    [5] 解析并重建 TXT
    [6] 解析指定页码范围
    [7] 查看帮助
    [8] 查看版本
    [9] 检查/配置凭据
    [A] 打开输出目录
    [S] 查看进度状态
    [T] 运行单元测试
    [R] 打开 README
    [0] 退出
------------------------------------------------------------
    退出码：0 成功 / 1 参数错误 / 2 运行时错误 / 130 用户中断
    首次使用请先执行 [9] 配置凭据；.env 不会被提交到仓库。
============================================================"""

HELP_TEXT = """\
============================================================
    查看帮助
============================================================
    [1] 顶层帮助（示例 / 凭据 / 退出码）
    [2] crawl 帮助（抓取参数）
    [3] parse 帮助（解析参数）
    [0] 返回主菜单
============================================================"""


def _configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def _clear() -> None:
    if not sys.stdout.isatty():
        return
    try:
        os.system("cls" if os.name == "nt" else "clear")
    except OSError:
        pass


def _read_key(prompt: str) -> str:
    """读取一个按键：Windows 控制台下无需回车；管道/非交互环境退回 input()。"""
    print(prompt, end="", flush=True)
    if os.name == "nt" and sys.stdin.isatty():
        try:
            import msvcrt
        except ImportError:
            pass
        else:
            while True:
                char = msvcrt.getwch()
                if char in ("\x00", "\xe0"):  # 功能键前缀
                    msvcrt.getwch()
                    continue
                if char in ("\r", "\n"):
                    continue
                print(char)
                return char
    try:
        return input().strip()[:1]
    except EOFError:
        return "0"


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def _pause() -> None:
    try:
        input("\n按回车返回菜单...")
    except EOFError:
        pass


def _exit_banner(code: int) -> None:
    print("\n" + "-" * 60)
    print(f"退出码：{code}（{EXIT_MEANINGS.get(code, '未知')}）")
    print("-" * 60)


def _run_main(args: List[str]) -> int:
    command = [sys.executable, str(MAIN_PY)] + args
    print("\n>> " + " ".join(command) + "\n")
    try:
        code = subprocess.call(command, cwd=str(BASE_DIR))
    except OSError as exc:
        print(f"[错误] 无法启动 main.py：{exc}")
        code = 2
    _exit_banner(code)
    _pause()
    return code


def _ask_tid() -> Tuple[bool, Optional[str]]:
    value = _ask("请输入 tid（直接回车使用 config/targets.py 的默认值）：")
    if not value:
        return True, None
    if not value.isdigit() or int(value) <= 0:
        print("\n[错误] tid 必须为正整数。")
        _pause()
        return False, None
    return True, value


def _ask_pages(default: str = "all") -> Tuple[bool, str]:
    value = _ask(f"请输入页码范围（如 1-20 / 21- / all，直接回车={default}）：") or default
    if not _PAGES_RE.match(value):
        print("\n[错误] 页码范围非法：支持 all / N / A-B / A-。")
        _pause()
        return False, default
    return True, value


def _with_tid(args: List[str], tid: Optional[str]) -> List[str]:
    return args + (["--tid", tid] if tid else [])


def action_crawl_all() -> None:
    ok, tid = _ask_tid()
    if not ok:
        return
    _run_main(_with_tid(["crawl"], tid) + ["--pages", "all"])


def action_crawl_pages() -> None:
    ok, tid = _ask_tid()
    if not ok:
        return
    ok, pages = _ask_pages("all")
    if not ok:
        return
    _run_main(_with_tid(["crawl"], tid) + ["--pages", pages])


def action_crawl_resume() -> None:
    ok, tid = _ask_tid()
    if not ok:
        return
    _run_main(_with_tid(["crawl"], tid))


def action_crawl_force() -> None:
    ok, tid = _ask_tid()
    if not ok:
        return
    _run_main(_with_tid(["crawl"], tid) + ["--force"])


def action_parse_all() -> None:
    ok, tid = _ask_tid()
    if not ok:
        return
    _run_main(_with_tid(["parse"], tid) + ["--format", "txt", "--force"])


def action_parse_pages() -> None:
    ok, tid = _ask_tid()
    if not ok:
        return
    ok, pages = _ask_pages("all")
    if not ok:
        return
    _run_main(_with_tid(["parse"], tid) + ["--pages", pages, "--format", "txt", "--force"])


def action_help() -> None:
    while True:
        _clear()
        print(HELP_TEXT)
        print()
        choice = _read_key("请选择: ").strip().lower()
        if choice == "0":
            return
        if choice == "1":
            _run_main(["-h"])
            return
        if choice == "2":
            _run_main(["crawl", "-h"])
            return
        if choice == "3":
            _run_main(["parse", "-h"])
            return


def action_version() -> None:
    _run_main(["-V"])


def action_credentials() -> None:
    _clear()
    print("=" * 60)
    print("    凭据配置检查（只显示是否设置，不显示具体值）")
    print("=" * 60)
    print("    .env 文件：" + ("已存在" if ENV_FILE.exists() else "未找到"))
    for name in ("NGA_COOKIE", "NGA_UID", "NGA_CID"):
        state = "已设置" if os.environ.get(name, "").strip() else "未设置"
        print(f"    环境变量 {name}：{state}")
    print()
    if not ENV_FILE.exists():
        choice = _read_key("是否从 .env.example 创建 .env？[Y/N]: ").strip().lower()
        if choice == "y":
            try:
                shutil.copyfile(ENV_EXAMPLE, ENV_FILE)
            except OSError as exc:
                print(f"\n[错误] 创建 .env 失败：{exc}")
            else:
                print("\n已创建 .env。请用记事本填入 NGA_COOKIE，或 NGA_UID + NGA_CID。")
        else:
            print("\n已取消创建。也可以直接设置同名环境变量。")
    else:
        print("请确认 .env 中 NGA_COOKIE（或 NGA_UID + NGA_CID）已有值。")
    print("\n提示：可用 [2] 抓 1 页快速验证凭据是否有效；失效会立即退出 2。")
    print(".env 已加入 .gitignore，不会提交到仓库。")
    _pause()


def action_open_output() -> None:
    try:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"[错误] 创建输出目录失败：{exc}")
        _pause()
        return
    _open_path(OUTPUT_DIR)


def action_show_state() -> None:
    _clear()
    print("=" * 60)
    print("    抓取进度（state.json）")
    print("=" * 60)
    if not STATE_FILE.exists():
        print("    尚无 state.json：还没有成功抓取过，或尚未运行 crawl。")
        _pause()
        return
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"    读取失败：{exc}")
        _pause()
        return
    threads = data.get("threads") if isinstance(data, dict) else None
    if not isinstance(threads, dict) or not threads:
        print("    state.json 中暂无进度记录。")
        _pause()
        return
    for tid in sorted(threads, key=str):
        info = threads[tid] if isinstance(threads[tid], dict) else {}
        print(
            f"    tid={tid}  last_page={info.get('last_page', 0)}  "
            f"failed_pages={info.get('failed_pages', [])}  "
            f"updated_at={info.get('updated_at') or '-'}"
        )
    _pause()


def action_run_tests() -> None:
    command = [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", "."]
    print("\n>> " + " ".join(command) + "\n")
    try:
        code = subprocess.call(command, cwd=str(BASE_DIR))
    except OSError as exc:
        print(f"[错误] 无法运行测试：{exc}")
        code = 2
    print("\n" + "-" * 60)
    print(f"测试退出码：{code}（{'通过' if code == 0 else '失败'}）")
    print("-" * 60)
    _pause()


def action_open_readme() -> None:
    _open_path(BASE_DIR / "README.md")


def _open_path(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.call(["open", str(path)])
        else:
            subprocess.call(["xdg-open", str(path)])
    except OSError as exc:
        print(f"[错误] 无法打开 {path}：{exc}")
        _pause()


ACTIONS = {
    "1": action_crawl_all,
    "2": action_crawl_pages,
    "3": action_crawl_resume,
    "4": action_crawl_force,
    "5": action_parse_all,
    "6": action_parse_pages,
    "7": action_help,
    "8": action_version,
    "9": action_credentials,
    "A": action_open_output,
    "S": action_show_state,
    "T": action_run_tests,
    "R": action_open_readme,
}


def main() -> int:
    if not MAIN_PY.exists():
        print(f"[错误] 未找到 {MAIN_PY}")
        return 2
    _configure_output()
    try:
        while True:
            _clear()
            print(MENU_TEXT)
            print()
            choice = _read_key("请输入菜单编号: ").strip().upper()
            if choice == "0":
                print("\n已退出。祝抓取顺利。")
                return 0
            action = ACTIONS.get(choice)
            if action is not None:
                action()
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
