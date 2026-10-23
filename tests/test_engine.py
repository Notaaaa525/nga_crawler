"""engine 单元测试：区间解析、state、crawl/parse 两个循环、架构纪律。"""
from __future__ import annotations

import ast
import json
import logging
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import crawler  # noqa: E402
import engine  # noqa: E402
import log as log_module  # noqa: E402
from config.runtime import RuntimeConfig  # noqa: E402
from config.targets import TargetConfig  # noqa: E402


def setUpModule() -> None:
    root = logging.getLogger()
    root.addHandler(logging.NullHandler())
    root.setLevel(logging.WARNING)


def make_cfg(tmp: Path, **overrides) -> RuntimeConfig:
    options = dict(
        delay_seconds=0.0,
        max_retries=1,
        backoff_429_seconds=0.0,
        max_consecutive_429=3,
        timeout=1.0,
        cache_dir=tmp / "cache",
        log_level="WARNING",
        log_path=tmp / "nga.log",
    )
    options.update(overrides)
    return RuntimeConfig(**options)


def make_record(tid: int, page: int, total_pages: int = 2) -> dict:
    data = {
        "__T": {"subject": f"标题-{tid}", "authorid": 111, "author": "楼主甲"},
        "__R": [
            {
                "lou": 0,
                "pid": 1000 + page,
                "authorid": 111,
                "author": "楼主甲",
                "subject": "主楼",
                "content": "主楼正文第一段\n\n主楼正文第二段",
                "postdate": "2024-04-12 11:27",
            }
        ],
        "__U": {"111": {"username": "楼主甲"}},
        "__ROWS": total_pages * 20,
        "__R__ROWS_PAGE": 20,
        "__PAGE": {"1": total_pages, "2": page, "3": 20},
    }
    return {
        "tid": tid,
        "page": page,
        "url": f"https://bbs.nga.cn/read.php?tid={tid}&page={page}&__output=11",
        "fetched_at": "2026-10-23T12:00:00+08:00",
        "status": "ok",
        "req_id": f"{tid}-{page}-deadbeef",
        "page_type": "thread",
        "total_pages": total_pages,
        "current_page": page,
        "next_url": None,
        "fetcher_version": "1.0.0",
        "content": {"source": "json", "data": data},
    }


def write_cache(cache_dir: Path, tid: int, page: int, record: dict) -> Path:
    directory = cache_dir / str(tid)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"page_{page:03d}.json"
    path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    return path


def read_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


class ParsePageRangeTest(unittest.TestCase):
    def test_all(self) -> None:
        self.assertEqual(engine.parse_page_range("all"), engine.PageRange(1, None))
        self.assertEqual(engine.parse_page_range("ALL"), engine.PageRange(1, None))

    def test_single(self) -> None:
        self.assertEqual(engine.parse_page_range("3"), engine.PageRange(3, 3))

    def test_closed(self) -> None:
        self.assertEqual(engine.parse_page_range("1-5"), engine.PageRange(1, 5))

    def test_open_ended(self) -> None:
        self.assertEqual(engine.parse_page_range("1-"), engine.PageRange(1, None))

    def test_whitespace(self) -> None:
        self.assertEqual(engine.parse_page_range("  1-5  "), engine.PageRange(1, 5))

    def test_invalid(self) -> None:
        for spec in ("0", "5-3", "-5", "", "1-2-3", "abc", "1.5", "all-2"):
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError):
                    engine.parse_page_range(spec)


class ResolvePageRangeTest(unittest.TestCase):
    def test_explicit_overrides_state(self) -> None:
        page_range = engine.resolve_page_range(
            "21-", total_pages=30, failed_pages=[3], last_page=20
        )
        self.assertEqual((page_range.start, page_range.end), (21, 30))

    def test_resume_from_failed(self) -> None:
        page_range = engine.resolve_page_range(None, failed_pages=[7, 3], last_page=20)
        self.assertEqual(page_range.start, 3)
        self.assertIsNone(page_range.end)

    def test_resume_from_last_page(self) -> None:
        page_range = engine.resolve_page_range(None, failed_pages=[], last_page=20)
        self.assertEqual(page_range.start, 21)

    def test_force_starts_at_one(self) -> None:
        page_range = engine.resolve_page_range(None, last_page=20, force=True)
        self.assertEqual(page_range.start, 1)
        self.assertIsNone(page_range.end)

    def test_max_pages_unknown_total(self) -> None:
        page_range = engine.resolve_page_range("1-100", max_pages=5)
        self.assertEqual((page_range.start, page_range.end), (1, 5))

    def test_max_pages_known_total(self) -> None:
        page_range = engine.resolve_page_range("1-100", max_pages=5, total_pages=2)
        self.assertEqual((page_range.start, page_range.end), (1, 2))

    def test_total_truncates_end(self) -> None:
        page_range = engine.resolve_page_range("1-20", total_pages=10)
        self.assertEqual((page_range.start, page_range.end), (1, 10))

    def test_start_beyond_total_is_empty(self) -> None:
        page_range = engine.resolve_page_range("21-", total_pages=10)
        self.assertTrue(page_range.empty)

    def test_total_unknown_keeps_open_end(self) -> None:
        page_range = engine.resolve_page_range("3-")
        self.assertEqual((page_range.start, page_range.end), (3, None))


class StateTest(unittest.TestCase):
    def test_missing_state_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            state = engine.load_state(Path(tmp_name) / "state.json")
            self.assertEqual(state, {"version": 1, "threads": {}})

    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            path = Path(tmp_name) / "state.json"
            state = {"version": 1, "threads": {"123": {"last_page": 3, "failed_pages": [2]}}}
            engine.save_state(path, state)
            loaded = engine.load_state(path)
            self.assertEqual(loaded["threads"]["123"]["last_page"], 3)
            self.assertEqual(loaded["threads"]["123"]["failed_pages"], [2])

    def test_corrupted_state_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            path = Path(tmp_name) / "state.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertLogs("engine", level="WARNING"):
                state = engine.load_state(path)
            self.assertEqual(state, {"version": 1, "threads": {}})

    def test_structurally_invalid_state_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            path = Path(tmp_name) / "state.json"
            path.write_text('["wrong"]', encoding="utf-8")
            with self.assertLogs("engine", level="WARNING"):
                state = engine.load_state(path)
            self.assertEqual(state["threads"], {})

    def test_save_failure_raises_state_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            root = Path(tmp_name)
            # 让临时文件路径被目录占用，迫使 open() 失败
            (root / "state.json.tmp").mkdir()
            with self.assertRaises(engine.StateError):
                engine.save_state(root / "state.json", {"version": 1, "threads": {}})

    def test_mark_helpers(self) -> None:
        entry = {"last_page": 0, "failed_pages": []}
        self.assertTrue(engine._mark_failed(entry, 3))
        self.assertFalse(engine._mark_failed(entry, 3))
        self.assertTrue(engine._mark_success(entry, 3))
        self.assertEqual(entry["failed_pages"], [])
        self.assertEqual(entry["last_page"], 3)
        self.assertFalse(engine._mark_success(entry, 1))

    def test_select_retry_pages(self) -> None:
        state = {"threads": {"1": {"failed_pages": [5, 2, 2]}}}
        self.assertEqual(engine.select_retry_pages(state, 1), [2, 5])
        self.assertEqual(engine.select_retry_pages(state, 2), [])


class ParseLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = make_cfg(self.tmp)
        self.targets = TargetConfig(tid=123)
        self.output_dir = self.tmp / "output"
        self.args = Namespace(command="parse", pages=None, force=True, format="txt")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, args=None) -> int:
        return engine.parse_loop(
            args or self.args,
            cfg=self.cfg,
            targets_cfg=self.targets,
            output_dir=self.output_dir,
        )

    def test_no_cache_returns_two(self) -> None:
        self.assertEqual(self._run(), 2)

    def test_rebuild_output(self) -> None:
        write_cache(self.cfg.cache_dir, 123, 1, make_record(123, 1, total_pages=2))
        write_cache(self.cfg.cache_dir, 123, 2, make_record(123, 2, total_pages=2))
        code = self._run()
        self.assertEqual(code, 0)
        output = (self.output_dir / "123.txt").read_text(encoding="utf-8")
        self.assertIn("NGA 帖子导出", output)
        self.assertIn("TID: 123", output)
        self.assertIn("=" * 80, output)
        self.assertIn("===== 第 0 楼 | 楼主甲 | 2024-04-12 11:27 =====", output)
        self.assertIn("抓取时间: 2026-10-23 12:00:00 ~ 2026-10-23 12:00:00", output)
        self.assertIn("parser_version: 1.0.0", output)
        self.assertNotIn("deadbeef", output)  # req_id 不展示
        self.assertEqual(output.count("===== 第 0 楼"), 2)
        self.assertFalse((self.output_dir / "123.txt.body.tmp").exists())
        self.assertFalse((self.output_dir / "123.txt.tmp").exists())

    def test_missing_page_only_warns(self) -> None:
        write_cache(self.cfg.cache_dir, 123, 1, make_record(123, 1, total_pages=3))
        write_cache(self.cfg.cache_dir, 123, 3, make_record(123, 3, total_pages=3))
        with self.assertLogs("engine", level="WARNING") as captured:
            code = self._run()
        self.assertEqual(code, 0)
        self.assertTrue(any("缺少缓存页" in message for message in captured.output))
        output = (self.output_dir / "123.txt").read_text(encoding="utf-8")
        self.assertIn("本次收录页: 1-3", output)

    def test_no_thread_pages_returns_two(self) -> None:
        login_wall = make_record(123, 1)
        login_wall.update({"status": "auth_error", "page_type": "login_wall", "total_pages": None})
        login_wall["content"] = {"source": "html", "message": "未登录", "excerpt": ""}
        write_cache(self.cfg.cache_dir, 123, 1, login_wall)
        with self.assertLogs("engine", level="WARNING"):
            self.assertEqual(self._run(), 2)
        self.assertFalse((self.output_dir / "123.txt").exists())

    def test_pages_filter(self) -> None:
        write_cache(self.cfg.cache_dir, 123, 1, make_record(123, 1, total_pages=2))
        write_cache(self.cfg.cache_dir, 123, 2, make_record(123, 2, total_pages=2))
        args = Namespace(command="parse", pages="2", force=True, format="txt")
        self.assertEqual(self._run(args), 0)
        output = (self.output_dir / "123.txt").read_text(encoding="utf-8")
        self.assertIn("本次收录页: 2", output)
        self.assertEqual(output.count("===== 第 0 楼"), 1)

    def test_append_mode_keeps_header(self) -> None:
        write_cache(self.cfg.cache_dir, 123, 1, make_record(123, 1, total_pages=2))
        write_cache(self.cfg.cache_dir, 123, 2, make_record(123, 2, total_pages=2))
        self.assertEqual(self._run(), 0)
        append_args = Namespace(command="parse", pages="1", force=False, format="txt")
        self.assertEqual(self._run(append_args), 0)
        output = (self.output_dir / "123.txt").read_text(encoding="utf-8")
        self.assertEqual(output.count("NGA 帖子导出"), 1)
        self.assertEqual(output.count("===== 第 0 楼"), 3)

    def test_failed_replace_keeps_old_output(self) -> None:
        write_cache(self.cfg.cache_dir, 123, 1, make_record(123, 1, total_pages=1))
        output_path = self.output_dir / "123.txt"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        output_path.write_text("OLD CONTENT\n", encoding="utf-8")
        with mock.patch.object(engine.os, "replace", side_effect=OSError("busy")):
            code = self._run()
        self.assertEqual(code, 2)
        self.assertEqual(output_path.read_text(encoding="utf-8"), "OLD CONTENT\n")
        self.assertFalse((self.output_dir / "123.txt.tmp").exists())
        self.assertFalse((self.output_dir / "123.txt.body.tmp").exists())

    def test_keyboard_interrupt_returns_130(self) -> None:
        write_cache(self.cfg.cache_dir, 123, 1, make_record(123, 1, total_pages=1))
        with mock.patch.object(engine, "_process_pages", side_effect=KeyboardInterrupt):
            code = self._run()
        self.assertEqual(code, 130)
        self.assertFalse((self.output_dir / "123.txt.body.tmp").exists())

    def test_dedupe_by_pid(self) -> None:
        write_cache(self.cfg.cache_dir, 123, 1, make_record(123, 1, total_pages=2))
        duplicate = make_record(123, 2, total_pages=2)
        duplicate["content"]["data"]["__R"][0]["pid"] = 1000 + 1
        write_cache(self.cfg.cache_dir, 123, 2, duplicate)
        with self.assertLogs("engine", level="WARNING") as captured:
            self.assertEqual(self._run(), 0)
        self.assertTrue(any("重复帖子" in message for message in captured.output))
        output = (self.output_dir / "123.txt").read_text(encoding="utf-8")
        self.assertEqual(output.count("===== 第 0 楼"), 1)


class CrawlLoopTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = make_cfg(self.tmp)
        self.targets = TargetConfig(tid=555)
        self.state_path = self.tmp / "state.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _args(self, pages="1-2", force=False) -> Namespace:
        return Namespace(command="crawl", pages=pages, force=force, max_pages=0)

    def _run(self, args=None, fetch=None) -> int:
        with mock.patch.object(engine.time, "sleep"), mock.patch.object(
            crawler, "fetch_page", side_effect=fetch
        ):
            return engine.crawl_loop(
                args or self._args(),
                cfg=self.cfg,
                targets_cfg=self.targets,
                state_path=self.state_path,
            )

    def test_success_updates_state(self) -> None:
        calls = []

        def fetch(tid, page, *, cfg=None, req_id=None):
            calls.append(page)
            return make_record(tid, page, total_pages=2)

        code = self._run(fetch=fetch)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [1, 2])
        entry = read_state(self.state_path)["threads"]["555"]
        self.assertEqual(entry["last_page"], 2)
        self.assertEqual(entry["failed_pages"], [])

    def test_auth_error_returns_two_and_marks_failed(self) -> None:
        def fetch(tid, page, *, cfg=None, req_id=None):
            raise crawler.AuthError("HTTP 403")

        code = self._run(fetch=fetch)
        self.assertEqual(code, 2)
        entry = read_state(self.state_path)["threads"]["555"]
        self.assertIn(1, entry["failed_pages"])
        self.assertEqual(entry["last_page"], 0)

    def test_non_fatal_page_error_continues(self) -> None:
        calls = []

        def fetch(tid, page, *, cfg=None, req_id=None):
            calls.append(page)
            if page == 1:
                raise crawler.PageError(page=1, status=500, message="boom", fatal=False)
            return make_record(tid, page, total_pages=2)

        code = self._run(fetch=fetch)
        self.assertEqual(code, 2)
        self.assertEqual(calls, [1, 2])
        entry = read_state(self.state_path)["threads"]["555"]
        self.assertEqual(entry["failed_pages"], [1])
        self.assertEqual(entry["last_page"], 2)

    def test_fatal_page_error_stops(self) -> None:
        calls = []

        def fetch(tid, page, *, cfg=None, req_id=None):
            calls.append(page)
            raise crawler.PageError(page=page, status=404, message="主题不存在", fatal=True)

        code = self._run(fetch=fetch)
        self.assertEqual(code, 2)
        self.assertEqual(calls, [1])

    def test_clamped_page_ends_run_with_success(self) -> None:
        def fetch(tid, page, *, cfg=None, req_id=None):
            raise crawler.PageClampedError(requested=page, current=1, total=1)

        code = self._run(fetch=fetch)
        self.assertEqual(code, 0)
        entry = read_state(self.state_path)["threads"]["555"]
        self.assertEqual(entry["failed_pages"], [])

    def test_rate_limit_retried_once_by_engine(self) -> None:
        calls = []

        def fetch(tid, page, *, cfg=None, req_id=None):
            calls.append(page)
            if len(calls) == 1:
                raise crawler.RateLimitError("HTTP 429")
            return make_record(tid, page, total_pages=1)

        args = self._args(pages="1")
        code = self._run(args=args, fetch=fetch)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [1, 1])

    def test_consecutive_rate_limit_aborts(self) -> None:
        self.cfg = make_cfg(self.tmp, max_consecutive_429=2)

        def fetch(tid, page, *, cfg=None, req_id=None):
            raise crawler.RateLimitError("HTTP 429")

        code = self._run(args=self._args(pages="1-3"), fetch=fetch)
        self.assertEqual(code, 2)
        entry = read_state(self.state_path)["threads"]["555"]
        self.assertEqual(entry["failed_pages"], [1, 2])

    def test_resume_from_earliest_failed_page(self) -> None:
        self.state_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "threads": {"555": {"last_page": 5, "failed_pages": [3]}},
                }
            ),
            encoding="utf-8",
        )
        calls = []

        def fetch(tid, page, *, cfg=None, req_id=None):
            calls.append(page)
            return make_record(tid, page, total_pages=5)

        code = self._run(args=self._args(pages=None), fetch=fetch)
        self.assertEqual(code, 0)
        self.assertEqual(calls[0], 3)
        self.assertEqual(calls[-1], 5)
        entry = read_state(self.state_path)["threads"]["555"]
        self.assertEqual(entry["failed_pages"], [])
        self.assertEqual(entry["last_page"], 5)

    def test_force_bypasses_cache(self) -> None:
        cached = make_record(555, 1, total_pages=1)
        calls = []

        def fetch(tid, page, *, cfg=None, req_id=None):
            calls.append(page)
            return make_record(tid, page, total_pages=1)

        with mock.patch.object(crawler, "load_cached_page", return_value=cached):
            code = self._run(args=self._args(pages="1", force=True), fetch=fetch)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [1])

    def test_keyboard_interrupt_returns_130(self) -> None:
        def fetch(tid, page, *, cfg=None, req_id=None):
            raise KeyboardInterrupt

        code = self._run(fetch=fetch)
        self.assertEqual(code, 130)
        entry = read_state(self.state_path)["threads"]["555"]
        self.assertIn(1, entry["failed_pages"])

    def test_state_save_failure_returns_two(self) -> None:
        def fetch(tid, page, *, cfg=None, req_id=None):
            return make_record(tid, page, total_pages=1)

        with mock.patch.object(engine, "save_state", side_effect=engine.StateError("busy")):
            code = self._run(args=self._args(pages="1"), fetch=fetch)
        self.assertEqual(code, 2)


class CacheSkipTest(unittest.TestCase):
    """单独测试缓存命中分支（避免 mock 返回值破坏契约）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.cfg = make_cfg(self.tmp)
        self.targets = TargetConfig(tid=555)
        self.state_path = self.tmp / "state.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_cache_hit_skips_network(self) -> None:
        cached = make_record(555, 1, total_pages=1)
        args = Namespace(command="crawl", pages="1", force=False, max_pages=0)
        with mock.patch.object(crawler, "load_cached_page", return_value=cached), \
                mock.patch.object(crawler, "fetch_page") as fetch_page_mock, \
                mock.patch.object(engine.time, "sleep"):
            code = engine.crawl_loop(
                args,
                cfg=self.cfg,
                targets_cfg=self.targets,
                state_path=self.state_path,
            )
        self.assertEqual(code, 0)
        fetch_page_mock.assert_not_called()
        entry = read_state(self.state_path)["threads"]["555"]
        self.assertEqual(entry["last_page"], 1)


class LogUtilitiesTest(unittest.TestCase):
    def test_mask_secret(self) -> None:
        self.assertEqual(log_module.mask_secret("abcd1234efgh"), "abcd***")
        self.assertEqual(log_module.mask_secret("abc"), "a***")
        self.assertEqual(log_module.mask_secret(""), "***")
        self.assertEqual(log_module.mask_secret(None), "***")

    def test_new_req_id_format(self) -> None:
        req_id = log_module.new_req_id(123, 4)
        parts = req_id.split("-")
        self.assertEqual(parts[0], "123")
        self.assertEqual(parts[1], "4")
        self.assertEqual(len(parts[2]), 8)

    def test_request_context(self) -> None:
        self.assertEqual(log_module.current_req_id(), "-")
        with log_module.request_context("abc"):
            self.assertEqual(log_module.current_req_id(), "abc")
        self.assertEqual(log_module.current_req_id(), "-")

    def test_setup_logging_writes_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_name:
            root = logging.getLogger()
            path = Path(tmp_name) / "logs" / "nga.log"
            try:
                log_module.setup_logging("INFO", path)
                first = [h for h in root.handlers if getattr(h, "_nga_crawler_handler", False)]
                self.assertEqual(len(first), 2)
                log_module.setup_logging("INFO", path)
                second = [h for h in root.handlers if getattr(h, "_nga_crawler_handler", False)]
                self.assertEqual(len(second), 2)
                with log_module.request_context("req-1"):
                    logging.getLogger("crawler").info("hello")
                content = path.read_text(encoding="utf-8")
                self.assertIn("hello", content)
                self.assertIn("req-1", content)
            finally:
                for handler in list(root.handlers):
                    if getattr(handler, "_nga_crawler_handler", False):
                        root.removeHandler(handler)
                        handler.close()
                for existing in list(root.filters):
                    if isinstance(existing, log_module._ReqIdFilter):
                        root.removeFilter(existing)


class ArchitectureTest(unittest.TestCase):
    FORBIDDEN = {"urllib", "http", "socket", "requests", "crawler"}

    def _imports(self, name: str):
        tree = ast.parse((PROJECT_ROOT / f"{name}.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        return imported

    def test_parser_render_have_no_network_imports(self) -> None:
        for name in ("parser", "render"):
            with self.subTest(module=name):
                forbidden = self._imports(name) & self.FORBIDDEN
                self.assertEqual(forbidden, set(), f"{name}.py 引入了禁止模块：{forbidden}")

    def test_cli_does_not_import_network_directly(self) -> None:
        forbidden = self._imports("cli") & {"urllib", "socket", "requests", "http"}
        self.assertEqual(forbidden, set())

    def test_parser_render_have_no_file_io(self) -> None:
        for name in ("parser", "render"):
            tree = ast.parse((PROJECT_ROOT / f"{name}.py").read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    with self.subTest(module=name, call=node.func.id):
                        self.assertNotEqual(node.func.id, "open")


if __name__ == "__main__":
    unittest.main()
