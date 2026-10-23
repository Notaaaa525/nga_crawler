"""cli 单元测试：帮助、参数错误、退出码映射与子命令分发。"""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import cli  # noqa: E402
import engine  # noqa: E402
import log as log_module  # noqa: E402
from config import auth as auth_config  # noqa: E402
from config import runtime as runtime_config  # noqa: E402
from config import targets as targets_config  # noqa: E402


def _capture_main(argv):
    """返回 (exit_code, stdout, stderr)；用于断言流分离。"""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


def _capture_help(argv):
    """执行 -h/--version 之类的 SystemExit 命令，返回 (code, stdout, stderr)。"""
    stdout, stderr = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            code = cli.main(argv)
        except SystemExit as exc:
            return exc.code, stdout.getvalue(), stderr.getvalue()
    raise AssertionError(f"{argv!r} 未抛出 SystemExit，实际返回 {code!r}")


def _silent_main(argv):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return cli.main(argv)


class HelpTest(unittest.TestCase):
    def test_help_exits_zero(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            with self.assertRaises(SystemExit) as context:
                cli.main(["--help"])
        self.assertEqual(context.exception.code, 0)
        self.assertIn("crawl", stdout.getvalue())
        self.assertIn("parse", stdout.getvalue())

    def test_subcommand_help(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as context:
                cli.main(["crawl", "--help"])
        self.assertEqual(context.exception.code, 0)

    def test_no_subcommand_returns_one(self) -> None:
        self.assertEqual(_silent_main([]), 1)


class UsageErrorTest(unittest.TestCase):
    def test_invalid_pages_returns_one(self) -> None:
        self.assertEqual(_silent_main(["crawl", "--pages", "5-3"]), 1)

    def test_empty_pages_returns_one(self) -> None:
        self.assertEqual(_silent_main(["parse", "--pages", "abc"]), 1)

    def test_format_json_returns_one(self) -> None:
        self.assertEqual(_silent_main(["parse", "--format", "json"]), 1)

    def test_invalid_tid_returns_one(self) -> None:
        self.assertEqual(_silent_main(["crawl", "--tid", "0"]), 1)
        self.assertEqual(_silent_main(["crawl", "--tid", "abc"]), 1)

    def test_negative_delay_returns_one(self) -> None:
        self.assertEqual(_silent_main(["crawl", "--delay", "-1"]), 1)

    def test_missing_default_tid_returns_one(self) -> None:
        with mock.patch.object(targets_config, "DEFAULT_TID", None):
            self.assertEqual(_silent_main(["crawl"]), 1)

    def test_unknown_option_returns_one(self) -> None:
        self.assertEqual(_silent_main(["crawl", "--nope"]), 1)

    def test_missing_command_for_required_option(self) -> None:
        self.assertEqual(_silent_main(["--tid", "1"]), 1)


class ConfigErrorTest(unittest.TestCase):
    def test_missing_credentials_returns_two(self) -> None:
        with mock.patch.object(
            engine, "run_crawl", side_effect=auth_config.AuthConfigError("无凭据")
        ):
            self.assertEqual(_silent_main(["crawl", "--tid", "123"]), 2)

    def test_runtime_config_error_returns_two(self) -> None:
        from config.runtime import RuntimeConfigError

        with mock.patch.object(
            engine, "run_crawl", side_effect=RuntimeConfigError("坏环境变量")
        ):
            self.assertEqual(_silent_main(["crawl", "--tid", "123"]), 2)

    def test_log_setup_error_returns_two(self) -> None:
        with mock.patch.object(
            engine, "run_parse", side_effect=log_module.LogSetupError("无法写日志")
        ):
            self.assertEqual(_silent_main(["parse", "--tid", "123"]), 2)


class DispatchTest(unittest.TestCase):
    def test_crawl_dispatch(self) -> None:
        with mock.patch.object(engine, "run_crawl", return_value=0) as run_crawl_mock:
            self.assertEqual(_silent_main(["crawl", "--tid", "123"]), 0)
        self.assertEqual(run_crawl_mock.call_count, 1)
        namespace = run_crawl_mock.call_args.args[0]
        self.assertEqual(namespace.command, "crawl")
        self.assertEqual(namespace.tid, 123)

    def test_parse_dispatch(self) -> None:
        with mock.patch.object(engine, "run_parse", return_value=0) as run_parse_mock:
            self.assertEqual(_silent_main(["parse", "--tid", "123", "--force"]), 0)
        namespace = run_parse_mock.call_args.args[0]
        self.assertEqual(namespace.command, "parse")
        self.assertTrue(namespace.force)
        self.assertEqual(namespace.format, "txt")

    def test_exit_code_passthrough(self) -> None:
        with mock.patch.object(engine, "run_crawl", return_value=2):
            self.assertEqual(_silent_main(["crawl", "--tid", "123"]), 2)

    def test_keyboard_interrupt_returns_130(self) -> None:
        with mock.patch.object(engine, "run_parse", side_effect=KeyboardInterrupt):
            self.assertEqual(_silent_main(["parse", "--tid", "123"]), 130)

    def test_unexpected_exception_returns_two(self) -> None:
        with mock.patch.object(engine, "run_parse", side_effect=RuntimeError("boom")):
            self.assertEqual(_silent_main(["parse", "--tid", "123"]), 2)

    def test_options_translated(self) -> None:
        with mock.patch.object(engine, "run_crawl", return_value=0) as run_crawl_mock:
            _silent_main(
                [
                    "crawl",
                    "--tid",
                    "123",
                    "--pages",
                    "1-5",
                    "--force",
                    "--delay",
                    "3",
                    "--retries",
                    "1",
                    "--backoff-429",
                    "10",
                    "--timeout",
                    "5",
                ]
            )
        namespace = run_crawl_mock.call_args.args[0]
        self.assertEqual(namespace.pages, "1-5")
        self.assertTrue(namespace.force)
        self.assertEqual(namespace.delay, 3.0)
        self.assertEqual(namespace.retries, 1)
        self.assertEqual(namespace.backoff_429, 10.0)
        self.assertEqual(namespace.timeout, 5.0)


class HelpUxTest(unittest.TestCase):
    """交互改进的回归：帮助可自助、本地化、错误提示可操作。"""

    def test_top_help_is_localized_and_actionable(self) -> None:
        code, stdout, stderr = _capture_help(["--help"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        text = stdout
        self.assertIn("用法:", text)
        self.assertNotIn("usage:", text)
        self.assertIn("位置参数", text)
        self.assertIn("选项", text)
        self.assertNotIn("positional arguments:", text)
        self.assertNotIn("options:", text)
        self.assertIn("示例:", text)
        self.assertIn("NGA_COOKIE", text)
        self.assertIn("退出码", text)
        self.assertIn("crawl", text)
        self.assertIn("parse", text)

    def test_version_exits_zero(self) -> None:
        code, stdout, stderr = _capture_help(["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("NGA 特化爬虫", stdout)
        self.assertIn(runtime_config.PARSER_VERSION, stdout)
        self.assertIn(runtime_config.FETCHER_VERSION, stdout)

    def test_crawl_help_shows_defaults_and_env_names(self) -> None:
        code, stdout, _ = _capture_help(["crawl", "-h"])
        self.assertEqual(code, 0)
        self.assertIn("默认 2.0", stdout)
        self.assertIn("NGA_DELAY_SECONDS", stdout)
        self.assertIn("NGA_MAX_RETRIES", stdout)
        self.assertIn("--pages", stdout)
        self.assertIn("续跑", stdout)
        self.assertIn("--backoff-429", stdout)
        self.assertIn("默认 15", stdout)

    def test_parse_help_mentions_offline(self) -> None:
        code, stdout, _ = _capture_help(["parse", "-h"])
        self.assertEqual(code, 0)
        self.assertIn("不联网", stdout)
        self.assertIn("--force", stdout)
        self.assertIn("默认 all", stdout)

    def test_no_subcommand_help_goes_to_stderr(self) -> None:
        code, stdout, stderr = _capture_main([])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("用法:", stderr)
        self.assertIn("请指定子命令", stderr)
        self.assertIn("crawl", stderr)

    def test_usage_error_has_next_step_hint(self) -> None:
        code, stdout, stderr = _capture_main(["crawl", "--pages", "5-3"])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("参数错误", stderr)
        self.assertIn("用法:", stderr)
        self.assertIn("main.py crawl", stderr)
        self.assertIn("提示：", stderr)
        self.assertIn("python main.py crawl -h", stderr)

    def test_display_width_counts_cjk_as_two_columns(self) -> None:
        self.assertEqual(cli._display_width("abc"), 3)
        self.assertEqual(cli._display_width("中文"), 4)
        self.assertEqual(cli._display_width("a中"), 3)

    def test_wrap_respects_display_width_and_punctuation_rules(self) -> None:
        text = (
            "这是一段用于验证按显示宽度折行的中文帮助文本，"
            "需要保证收尾标点不落在行首，例如“；”与“）”；"
            "同时开头标点不留在行尾，例如“（”。"
        )
        lines = cli._wrap_display_width(text, 24)
        self.assertGreater(len(lines), 1)
        for line in lines:
            self.assertLessEqual(cli._display_width(line), 26)
            self.assertNotIn(line[0], cli._CLOSING_PUNCT)
            self.assertNotIn(line[-1], cli._OPENING_PUNCT)

    def test_missing_credentials_message_is_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            missing_env = Path(tmp_name) / "missing.env"
            with mock.patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(auth_config.AuthConfigError) as context:
                    auth_config.load_auth(missing_env)
            message = str(context.exception)
            self.assertIn("NGA_COOKIE", message)
            self.assertIn("NGA_UID", message)
            self.assertIn("NGA_CID", message)
            self.assertIn(".env", message)
            self.assertIn("document.cookie", message)


if __name__ == "__main__":
    unittest.main()
