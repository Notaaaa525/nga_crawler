"""crawler 单元测试：全部离线，mock ``crawler.urlopen`` 与 ``crawler.time.sleep``。"""
from __future__ import annotations

import gzip
import io
import json
import logging
import socket
import tempfile
import unittest
import zlib
from datetime import datetime
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

import crawler
from config.auth import Auth
from config.runtime import RuntimeConfig


TID = 123


def make_cfg(tmp: Path, **overrides: object) -> RuntimeConfig:
    """按 runtime.py 字段顺序直接构造运行配置。"""
    kwargs = dict(
        delay_seconds=0.0,
        max_retries=2,
        backoff_429_seconds=0.0,
        max_consecutive_429=3,
        timeout=1.0,
        cache_dir=Path(tmp),
        log_level="INFO",
        log_path=Path(tmp) / "nga.log",
    )
    kwargs.update(overrides)
    return RuntimeConfig(**kwargs)


AUTH = Auth(
    cookie="ngaPassportUid=test-uid; ngaPassportCid=test-cid-not-real",
    uid=None,
    cid=None,
    source="test",
)


class FakeResponse:
    """urllib response 的最小替身。"""

    def __init__(self, body: bytes = b"", status: int = 200, headers=None) -> None:
        self.body = body
        self.status = status
        self.headers = (
            dict(headers)
            if headers is not None
            else {"Content-Type": "application/json; charset=utf-8"}
        )
        self.read_count = 0
        self.closed = False

    def getcode(self) -> int:
        return self.status

    def read(self) -> bytes:
        self.read_count += 1
        return self.body

    def close(self) -> None:
        self.closed = True


def make_data(
    *,
    total: int = 2,
    current: int = 1,
    rows: int = 21,
    rows_page: int = 20,
    include_page: bool = True,
) -> dict:
    data = {
        "__T": {"subject": "测试帖子", "author": "楼主", "authorid": 1},
        "__R": [{"pid": 1001, "lou": 0, "authorid": 1, "content": "主楼正文"}],
        "__U": {"1": {"username": "楼主"}},
        "__ROWS": rows,
        "__R__ROWS_PAGE": rows_page,
    }
    if include_page:
        data["__PAGE"] = {"1": total, "2": current, "3": rows_page}
    return data


def wrap(data: dict, *, code: int = 0) -> dict:
    return {"code": code, "data": data}


def json_body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def page_path(cfg: RuntimeConfig, tid: int = TID, page: int = 1) -> Path:
    return Path(cfg.cache_dir) / str(tid) / f"page_{page:03d}.json"


def read_page(cfg: RuntimeConfig, tid: int = TID, page: int = 1) -> dict:
    return json.loads(page_path(cfg, tid, page).read_text(encoding="utf-8"))


class CrawlerTestBase(unittest.TestCase):
    """公共 setup：临时目录、直接构造的 RuntimeConfig、mock 凭据。"""

    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.cfg = make_cfg(self.tmp)
        # 测试期静音日志，避免 WARNING 污染输出。
        logging.disable(logging.CRITICAL)
        patcher = mock.patch.object(crawler, "load_auth", return_value=AUTH)
        self.load_auth_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        logging.disable(logging.NOTSET)
        self._tmpdir.cleanup()


# ---------------------------------------------------------------------------
# URL / 缓存 helper
# ---------------------------------------------------------------------------


class UrlAndCacheHelperTest(CrawlerTestBase):
    def test_build_url_default_base(self) -> None:
        self.assertEqual(
            crawler.build_url(123, 1),
            "https://bbs.nga.cn/read.php?tid=123&page=1&__output=11",
        )

    def test_build_url_parameter_order_and_custom_base(self) -> None:
        cfg = make_cfg(self.tmp, base_url="https://example.test/read.php")
        self.assertEqual(
            crawler.build_url(7, 10, cfg=cfg),
            "https://example.test/read.php?tid=7&page=10&__output=11",
        )

    def test_load_cached_page_missing_returns_none(self) -> None:
        self.assertIsNone(crawler.load_cached_page(TID, 1, self.cfg.cache_dir))

    def test_load_cached_page_corrupted_returns_none(self) -> None:
        path = page_path(self.cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not valid json", encoding="utf-8")
        self.assertIsNone(crawler.load_cached_page(TID, 1, self.cfg.cache_dir))

    def test_load_cached_page_valid_returns_dict(self) -> None:
        record = {"tid": TID, "page": 1, "status": "ok"}
        path = page_path(self.cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        self.assertEqual(crawler.load_cached_page(TID, 1, self.cfg.cache_dir), record)

    def test_is_usable_cache_rules(self) -> None:
        good = {
            "tid": TID,
            "page": 1,
            "status": "ok",
            "page_type": "thread",
            "total_pages": 3,
            "content": {"source": "json", "data": {"__R": []}},
        }
        self.assertTrue(crawler.is_usable_cache(good, TID, 1))

        bad_cases = [
            dict(good, tid=TID + 1),
            dict(good, page=2),
            dict(good, status="error"),
            dict(good, page_type="login_wall"),
            dict(good, total_pages="3"),
            dict(good, content={"source": "json", "data": []}),
            dict(good, content=None),
            dict(good, status="ok", page_type="thread", total_pages=3, content={}),
        ]
        for case in bad_cases:
            with self.subTest(case=case):
                self.assertFalse(crawler.is_usable_cache(case, TID, 1))
        self.assertFalse(crawler.is_usable_cache(None, TID, 1))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 成功路径
# ---------------------------------------------------------------------------


class FetchSuccessTest(CrawlerTestBase):
    def _fetch(self, body: bytes, **kwargs) -> dict:
        response = FakeResponse(body, headers={"Content-Type": "application/json; charset=utf-8"})
        with mock.patch.object(crawler, "urlopen", return_value=response) as opener:
            record = crawler.fetch_page(TID, 1, cfg=self.cfg, req_id="123-1-test", **kwargs)
        self.opener = opener
        return record

    def test_success_returns_record_and_writes_cache(self) -> None:
        record = self._fetch(json_body(wrap(make_data(total=2, current=1))))

        self.assertEqual(record["tid"], TID)
        self.assertEqual(record["page"], 1)
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["page_type"], "thread")
        self.assertEqual(record["total_pages"], 2)
        self.assertEqual(record["current_page"], 1)
        self.assertEqual(record["req_id"], "123-1-test")
        self.assertEqual(
            record["next_url"],
            "https://bbs.nga.cn/read.php?tid=123&page=2&__output=11",
        )
        self.assertEqual(record["content"]["source"], "json")
        self.assertEqual(record["content"]["data"]["__R"][0]["pid"], 1001)

        path = page_path(self.cfg)
        self.assertTrue(path.exists())
        saved = read_page(self.cfg)
        self.assertEqual(saved, record)
        # fetched_at：本地时区 ISO8601（含偏移）。
        fetched_at = datetime.fromisoformat(saved["fetched_at"])
        self.assertIsNotNone(fetched_at.tzinfo)

        request = self.opener.call_args[0][0]
        self.assertEqual(request.full_url, crawler.build_url(TID, 1, cfg=self.cfg))
        self.assertEqual(request.get_header("Cookie"), AUTH.cookie)
        self.assertIn("Mozilla", request.get_header("User-agent"))
        self.assertEqual(
            request.get_header("Accept"), "application/json, text/plain, */*"
        )
        self.assertEqual(request.get_header("Accept-encoding"), "identity")
        self.assertEqual(request.get_header("Referer"), "https://bbs.nga.cn/")
        self.assertEqual(self.opener.call_args.kwargs.get("timeout"), self.cfg.timeout)

    def test_success_top_level_data_shape(self) -> None:
        # 兼容“顶层就是 data”的返回结构。
        record = self._fetch(json_body(make_data(total=1, current=1, rows=5)))
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["total_pages"], 1)
        self.assertIsNone(record["next_url"])

    def test_success_gzip_and_charset_decoding(self) -> None:
        payload = json.dumps(
            wrap(make_data(total=1, current=1)), ensure_ascii=False
        ).encode("gb18030")
        body = gzip.compress(payload)
        response = FakeResponse(
            body,
            headers={
                "Content-Encoding": "gzip",
                "Content-Type": "application/json; charset=gb18030",
            },
        )
        with mock.patch.object(crawler, "urlopen", return_value=response):
            record = crawler.fetch_page(TID, 1, cfg=self.cfg)
        self.assertEqual(record["content"]["data"]["__T"]["subject"], "测试帖子")

    def test_success_deflate_decompression(self) -> None:
        payload = json_body(wrap(make_data(total=1, current=1)))
        response = FakeResponse(
            zlib.compress(payload),
            headers={
                "Content-Encoding": "deflate",
                "Content-Type": "application/json",
            },
        )
        with mock.patch.object(crawler, "urlopen", return_value=response):
            record = crawler.fetch_page(TID, 1, cfg=self.cfg)
        self.assertEqual(record["content"]["data"]["__T"]["subject"], "测试帖子")

    def test_pagination_fallback_from_rows_when_page_meta_missing(self) -> None:
        data = make_data(rows=41, rows_page=20, include_page=False)
        record = self._fetch(json_body(wrap(data)))
        self.assertEqual(record["total_pages"], 3)
        self.assertEqual(record["current_page"], 1)
        self.assertEqual(
            record["next_url"],
            "https://bbs.nga.cn/read.php?tid=123&page=2&__output=11",
        )

    def test_pagination_unavailable_falls_back_to_requested_page(self) -> None:
        data = {
            "__T": {},
            "__R": [],
            "__U": {},
        }
        record = self._fetch(json_body(wrap(data)))
        self.assertEqual(record["total_pages"], 1)
        self.assertEqual(record["current_page"], 1)
        self.assertIsNone(record["next_url"])

    def test_cfg_none_uses_resolve_runtime(self) -> None:
        response = FakeResponse(json_body(wrap(make_data(total=1, current=1))))
        with mock.patch.object(
            crawler, "resolve_runtime", return_value=self.cfg
        ) as resolve_mock, mock.patch.object(crawler, "urlopen", return_value=response):
            record = crawler.fetch_page(TID, 1)
        resolve_mock.assert_called_once_with(None)
        self.assertEqual(record["status"], "ok")

    def test_req_id_generated_when_missing(self) -> None:
        response = FakeResponse(json_body(wrap(make_data(total=1, current=1))))
        with mock.patch.object(
            crawler, "new_req_id", return_value="generated-1"
        ) as req_mock, mock.patch.object(crawler, "urlopen", return_value=response):
            record = crawler.fetch_page(TID, 1, cfg=self.cfg)
        req_mock.assert_called_once_with(TID, 1)
        self.assertEqual(record["req_id"], "generated-1")


# ---------------------------------------------------------------------------
# 认证 / 限流 / 永久错误
# ---------------------------------------------------------------------------


class FetchAuthAndLimitTest(CrawlerTestBase):
    def test_403_writes_auth_error_marker_and_does_not_retry(self) -> None:
        body = (
            "<html><body><form action='/login.php'>"
            "<input name='password'></form>请登录</body></html>"
        ).encode("utf-8")
        response = FakeResponse(body, status=403, headers={"Content-Type": "text/html"})
        with mock.patch.object(crawler, "urlopen", return_value=response) as opener, \
                mock.patch.object(crawler.time, "sleep") as sleep_mock:
            with self.assertRaises(crawler.AuthError):
                crawler.fetch_page(TID, 1, cfg=self.cfg)

        self.assertEqual(opener.call_count, 1)
        sleep_mock.assert_not_called()
        marker = read_page(self.cfg)
        self.assertEqual(marker["status"], "auth_error")
        self.assertEqual(marker["page_type"], "login_wall")
        self.assertIsNone(marker["total_pages"])
        self.assertEqual(marker["content"]["source"], "html")
        self.assertIn("403", marker["content"]["message"])

    def test_403_empty_body_uses_none_source(self) -> None:
        response = FakeResponse(b"", status=403, headers={"Content-Type": "text/html"})
        with mock.patch.object(crawler, "urlopen", return_value=response):
            with self.assertRaises(crawler.AuthError):
                crawler.fetch_page(TID, 1, cfg=self.cfg)
        marker = read_page(self.cfg)
        self.assertEqual(marker["status"], "auth_error")
        self.assertEqual(marker["content"]["source"], "none")
        self.assertEqual(marker["content"]["excerpt"], "")

    def test_429_raises_rate_limit_and_writes_no_cache(self) -> None:
        response = FakeResponse(b"", status=429)
        with mock.patch.object(crawler, "urlopen", return_value=response) as opener:
            with self.assertRaises(crawler.RateLimitError):
                crawler.fetch_page(TID, 1, cfg=self.cfg)
        self.assertEqual(opener.call_count, 1)
        self.assertFalse(page_path(self.cfg).exists())

    def test_json_code_2048_raises_rate_limit_and_writes_no_cache(self) -> None:
        body = json_body({"code": 2048, "msg": "请求过于频繁"})
        response = FakeResponse(body)
        with mock.patch.object(crawler, "urlopen", return_value=response) as opener:
            with self.assertRaises(crawler.RateLimitError):
                crawler.fetch_page(TID, 1, cfg=self.cfg)
        self.assertEqual(opener.call_count, 1)
        self.assertFalse(page_path(self.cfg).exists())

    def test_3xx_without_location_is_rate_limit_and_no_cache(self) -> None:
        response = FakeResponse(b"", status=302, headers={})
        with mock.patch.object(crawler, "urlopen", return_value=response):
            with self.assertRaises(crawler.RateLimitError):
                crawler.fetch_page(TID, 1, cfg=self.cfg)
        self.assertFalse(page_path(self.cfg).exists())

    def test_login_wall_html_raises_auth_error(self) -> None:
        body = (
            "<html><body><form action='/login.php' method='post'>"
            "<input type='text' name='username'>"
            "<input type='password' name='password'>"
            "</form>请登录</body></html>"
        ).encode("utf-8")
        response = FakeResponse(body, headers={"Content-Type": "text/html; charset=utf-8"})
        with mock.patch.object(crawler, "urlopen", return_value=response) as opener:
            with self.assertRaises(crawler.AuthError):
                crawler.fetch_page(TID, 1, cfg=self.cfg)
        self.assertEqual(opener.call_count, 1)
        marker = read_page(self.cfg)
        self.assertEqual(marker["status"], "auth_error")
        self.assertEqual(marker["page_type"], "login_wall")

    def test_json_login_message_raises_auth_error(self) -> None:
        body = json_body({"code": 1, "msg": "未登录，请先登录"})
        response = FakeResponse(body)
        with mock.patch.object(crawler, "urlopen", return_value=response):
            with self.assertRaises(crawler.AuthError):
                crawler.fetch_page(TID, 1, cfg=self.cfg)
        marker = read_page(self.cfg)
        self.assertEqual(marker["page_type"], "login_wall")

    def test_real_http_error_403_raises_auth_error(self) -> None:
        error = HTTPError(
            "https://bbs.nga.cn/read.php",
            403,
            "Forbidden",
            {"Content-Type": "text/html"},
            io.BytesIO("<html>请登录</html>".encode("utf-8")),
        )
        with mock.patch.object(crawler, "urlopen", side_effect=error) as opener:
            with self.assertRaises(crawler.AuthError):
                crawler.fetch_page(TID, 1, cfg=self.cfg)
        self.assertEqual(opener.call_count, 1)
        marker = read_page(self.cfg)
        self.assertEqual(marker["status"], "auth_error")
        self.assertEqual(marker["page_type"], "login_wall")

    def test_real_http_error_500_retries_then_nonfatal(self) -> None:
        self.cfg = make_cfg(self.tmp, max_retries=1)

        def build_error() -> HTTPError:
            return HTTPError(
                "https://bbs.nga.cn/read.php",
                500,
                "Server Error",
                {"Content-Type": "text/html"},
                io.BytesIO(b"<html>boom</html>"),
            )

        def raise_error(*args, **kwargs):
            raise build_error()

        with mock.patch.object(crawler, "urlopen", side_effect=raise_error) as opener, \
                mock.patch.object(crawler.time, "sleep") as sleep_mock:
            with self.assertRaises(crawler.PageError) as ctx:
                crawler.fetch_page(TID, 1, cfg=self.cfg)

        self.assertFalse(ctx.exception.fatal)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual([call.args[0] for call in sleep_mock.call_args_list], [2])
        marker = read_page(self.cfg)
        self.assertEqual(marker["page_type"], "error")

    def test_404_is_fatal_page_error(self) -> None:
        response = FakeResponse(
            "<html><body>主题不存在</body></html>".encode("utf-8"),
            status=404,
            headers={"Content-Type": "text/html; charset=utf-8"},
        )
        with mock.patch.object(crawler, "urlopen", return_value=response):
            with self.assertRaises(crawler.PageError) as ctx:
                crawler.fetch_page(TID, 1, cfg=self.cfg)
        self.assertTrue(ctx.exception.fatal)
        self.assertEqual(ctx.exception.status, 404)
        marker = read_page(self.cfg)
        self.assertEqual(marker["status"], "error")
        self.assertEqual(marker["page_type"], "error")

    def test_json_permanent_message_is_fatal(self) -> None:
        body = json_body({"code": 1, "msg": "主题不存在或已删除"})
        response = FakeResponse(body)
        with mock.patch.object(crawler, "urlopen", return_value=response) as opener:
            with self.assertRaises(crawler.PageError) as ctx:
                crawler.fetch_page(TID, 1, cfg=self.cfg)
        self.assertTrue(ctx.exception.fatal)
        self.assertEqual(opener.call_count, 1)


# ---------------------------------------------------------------------------
# 瞬时错误重试
# ---------------------------------------------------------------------------


class FetchRetryTest(CrawlerTestBase):
    def test_malformed_json_retries_then_nonfatal_page_error(self) -> None:
        self.cfg = make_cfg(self.tmp, max_retries=2)
        response = FakeResponse(b"{broken json", headers={"Content-Type": "application/json"})
        with mock.patch.object(crawler, "urlopen", return_value=response) as opener, \
                mock.patch.object(crawler.time, "sleep") as sleep_mock:
            with self.assertRaises(crawler.PageError) as ctx:
                crawler.fetch_page(TID, 1, cfg=self.cfg)

        self.assertFalse(ctx.exception.fatal)
        self.assertEqual(ctx.exception.page, 1)
        self.assertEqual(opener.call_count, 3)  # 首次 + 2 次重试
        self.assertEqual(
            [call.args[0] for call in sleep_mock.call_args_list], [2, 4]
        )
        marker = read_page(self.cfg)
        self.assertEqual(marker["status"], "error")
        self.assertEqual(marker["page_type"], "unknown")

    def test_structure_missing_retries_then_nonfatal(self) -> None:
        self.cfg = make_cfg(self.tmp, max_retries=1)
        body = json_body({"code": 0, "data": {"__T": {}}})  # 缺 __R
        response = FakeResponse(body)
        with mock.patch.object(crawler, "urlopen", return_value=response) as opener, \
                mock.patch.object(crawler.time, "sleep") as sleep_mock:
            with self.assertRaises(crawler.PageError) as ctx:
                crawler.fetch_page(TID, 1, cfg=self.cfg)
        self.assertFalse(ctx.exception.fatal)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual([call.args[0] for call in sleep_mock.call_args_list], [2])

    def test_timeout_retries_then_crawl_error_without_cache(self) -> None:
        self.cfg = make_cfg(self.tmp, max_retries=2)
        with mock.patch.object(
            crawler, "urlopen", side_effect=socket.timeout("timed out")
        ) as opener, mock.patch.object(crawler.time, "sleep") as sleep_mock:
            with self.assertRaises(crawler.CrawlError):
                crawler.fetch_page(TID, 1, cfg=self.cfg)

        self.assertEqual(opener.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in sleep_mock.call_args_list], [2, 4]
        )
        self.assertFalse(page_path(self.cfg).exists())

    def test_url_error_retries_then_crawl_error(self) -> None:
        self.cfg = make_cfg(self.tmp, max_retries=1)
        with mock.patch.object(
            crawler, "urlopen", side_effect=OSError("connection reset")
        ) as opener, mock.patch.object(crawler.time, "sleep") as sleep_mock:
            with self.assertRaises(crawler.CrawlError):
                crawler.fetch_page(TID, 1, cfg=self.cfg)
        self.assertEqual(opener.call_count, 2)
        self.assertEqual([call.args[0] for call in sleep_mock.call_args_list], [2])

    def test_5xx_retries_then_nonfatal_page_error_with_marker(self) -> None:
        self.cfg = make_cfg(self.tmp, max_retries=2)
        response = FakeResponse(
            b"<html><body>server error</body></html>",
            status=500,
            headers={"Content-Type": "text/html"},
        )
        with mock.patch.object(crawler, "urlopen", return_value=response) as opener, \
                mock.patch.object(crawler.time, "sleep") as sleep_mock:
            with self.assertRaises(crawler.PageError) as ctx:
                crawler.fetch_page(TID, 1, cfg=self.cfg)

        self.assertFalse(ctx.exception.fatal)
        self.assertEqual(ctx.exception.status, 500)
        self.assertEqual(opener.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in sleep_mock.call_args_list], [2, 4]
        )
        marker = read_page(self.cfg)
        self.assertEqual(marker["status"], "error")
        self.assertEqual(marker["page_type"], "error")
        self.assertIn("server error", marker["content"]["excerpt"])

    def test_5xx_with_empty_body_marker_source_none(self) -> None:
        self.cfg = make_cfg(self.tmp, max_retries=0)
        response = FakeResponse(b"", status=502, headers={})
        with mock.patch.object(crawler, "urlopen", return_value=response):
            with self.assertRaises(crawler.PageError):
                crawler.fetch_page(TID, 1, cfg=self.cfg)
        marker = read_page(self.cfg)
        self.assertEqual(marker["content"]["source"], "none")
        self.assertEqual(marker["content"]["excerpt"], "")


# ---------------------------------------------------------------------------
# 夹回 / 页码异常
# ---------------------------------------------------------------------------


class ClampTest(CrawlerTestBase):
    def test_clamped_page_raises_and_writes_no_cache(self) -> None:
        body = json_body(wrap(make_data(total=2, current=2)))
        response = FakeResponse(body)
        with mock.patch.object(crawler, "urlopen", return_value=response):
            with self.assertRaises(crawler.PageClampedError) as ctx:
                crawler.fetch_page(TID, 5, cfg=self.cfg)

        self.assertEqual(ctx.exception.requested, 5)
        self.assertEqual(ctx.exception.current, 2)
        self.assertEqual(ctx.exception.total, 2)
        self.assertFalse(page_path(self.cfg, TID, 5).exists())
        self.assertFalse(page_path(self.cfg, TID, 2).exists())

    def test_non_clamp_page_mismatch_writes_marker_and_nonfatal(self) -> None:
        # 请求第 3 页，服务端返回第 2 页，但 total=5（未越界）。
        body = json_body(wrap(make_data(total=5, current=2)))
        response = FakeResponse(body)
        with mock.patch.object(crawler, "urlopen", return_value=response):
            with self.assertRaises(crawler.PageError) as ctx:
                crawler.fetch_page(TID, 3, cfg=self.cfg)

        self.assertFalse(ctx.exception.fatal)
        marker = read_page(self.cfg, TID, 3)
        self.assertEqual(marker["status"], "error")
        self.assertEqual(marker["page_type"], "unknown")
        self.assertEqual(marker["current_page"], 2)
        self.assertEqual(marker["total_pages"], 5)
        self.assertIsNone(marker["next_url"])


# ---------------------------------------------------------------------------
# 原子写
# ---------------------------------------------------------------------------


class AtomicWriteTest(CrawlerTestBase):
    def test_replace_failure_retries_then_cache_write_error(self) -> None:
        body = json_body(wrap(make_data(total=1, current=1)))
        response = FakeResponse(body)
        with mock.patch.object(crawler, "urlopen", return_value=response), \
                mock.patch.object(
                    crawler.os, "replace", side_effect=OSError("file in use")
                ) as replace_mock, \
                mock.patch.object(crawler.time, "sleep") as sleep_mock:
            with self.assertRaises(crawler.CacheWriteError):
                crawler.fetch_page(TID, 1, cfg=self.cfg)

        # 首次 + 3 次重试。
        self.assertEqual(replace_mock.call_count, 4)
        self.assertEqual(
            [call.args[0] for call in sleep_mock.call_args_list], [0.2, 0.2, 0.2]
        )
        self.assertFalse(page_path(self.cfg).exists())
        self.assertFalse(
            page_path(self.cfg).with_name(page_path(self.cfg).name + ".tmp").exists()
        )

    def test_replace_failure_then_success_still_writes(self) -> None:
        body = json_body(wrap(make_data(total=1, current=1)))
        response = FakeResponse(body)
        real_replace = crawler.os.replace
        calls = {"count": 0}

        def flaky_replace(src, dst):
            calls["count"] += 1
            if calls["count"] < 3:
                raise OSError("busy")
            return real_replace(src, dst)

        with mock.patch.object(crawler, "urlopen", return_value=response), \
                mock.patch.object(crawler.os, "replace", side_effect=flaky_replace), \
                mock.patch.object(crawler.time, "sleep") as sleep_mock:
            record = crawler.fetch_page(TID, 1, cfg=self.cfg)

        self.assertEqual(record["status"], "ok")
        self.assertEqual(calls["count"], 3)
        self.assertEqual(
            [call.args[0] for call in sleep_mock.call_args_list], [0.2, 0.2]
        )
        self.assertTrue(page_path(self.cfg).exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
