"""nga_parser.py 单元测试：离线、确定性、只读 fixtures，不写任何 cache/output/logs。"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Iterable, Iterator, List

from nga_parser import (
    MAX_QUOTE_DEPTH,
    MISSING_AUTHOR,
    MISSING_BODY,
    MISSING_LOU,
    MISSING_TIME,
    Block,
    ParsedPage,
    ParsedPost,
    absolutize_url,
    bbcode_to_blocks,
    parse_cache,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIXTURE_NAMES = (
    "page_001.json",
    "page_missing_fields.json",
    "page_login_wall.json",
    "page_error.json",
)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def walk_blocks(blocks: Iterable[Block]) -> Iterator[Block]:
    """深度优先遍历 block 树。"""
    for block in blocks:
        yield block
        if block.kind == "quote":
            yield from walk_blocks(block.children)


def text_of(blocks: Iterable[Block]) -> str:
    """递归拼接文本/链接内容（不含图片 URL），用于断言。"""
    parts: List[str] = []
    for block in blocks:
        if block.kind in ("text", "link"):
            parts.append(block.text)
        elif block.kind == "quote":
            parts.append(text_of(block.children))
    return "".join(parts)


class ParseCacheFixtureTest(unittest.TestCase):
    """page_001.json：OP + 2 回复，含引用/图片/链接/编辑标记与缺失字段。"""

    def setUp(self) -> None:
        self.cache = load_fixture("page_001.json")
        self.page = parse_cache(self.cache)

    def test_page_metadata(self) -> None:
        self.assertFalse(self.page.skipped)
        self.assertIsNone(self.page.skip_reason)
        self.assertEqual(self.page.tid, 123)
        self.assertEqual(self.page.page, 1)
        self.assertEqual(self.page.status, "ok")
        self.assertEqual(self.page.page_type, "thread")
        self.assertEqual(self.page.total_pages, 2)
        self.assertEqual(self.page.fetcher_version, "1.0.0")
        self.assertEqual(self.page.req_id, "123-1-abc123")
        self.assertEqual(self.page.fetched_at, "2026-10-23T12:34:56+08:00")
        self.assertEqual(self.page.op_author, "楼主先生")
        # [b] 被去掉；[写作]/[短篇] 是非 BBCode 方括号文本，必须保留。
        self.assertEqual(self.page.title, "[写作] [短篇] 测试帖 第二行")

    def test_posts_count_and_op(self) -> None:
        self.assertEqual(len(self.page.posts), 3)
        op = self.page.posts[0]
        self.assertEqual(op.lou, 0)
        self.assertEqual(op.pid, 900000001)
        self.assertEqual(op.author, "楼主先生")
        self.assertEqual(op.author_id, 10001)
        self.assertEqual(op.posted_at, "2024-04-12 11:27")
        self.assertTrue(op.is_op)
        self.assertEqual(op.missing_fields, [])
        self.assertEqual(op.edited, "本帖最后由 楼主先生 于 2024-04-12 11:30 编辑")

    def test_op_blocks_quote_image_link(self) -> None:
        op = self.page.posts[0]
        kinds = [block.kind for block in op.blocks]
        self.assertIn("quote", kinds)
        self.assertIn("image", kinds)
        self.assertIn("link", kinds)

        quote = next(block for block in op.blocks if block.kind == "quote")
        self.assertEqual(len(quote.children), 1)
        self.assertIn("引用内容第一行", quote.children[0].text)
        self.assertIn("引用内容第二行", quote.children[0].text)

        image = next(block for block in op.blocks if block.kind == "image")
        self.assertEqual(
            image.text, "https://img.nga.cn/attachments/mon_202404/12/abc.jpg"
        )

        link = next(block for block in op.blocks if block.kind == "link")
        self.assertEqual(
            link.text, "站内链接 (https://bbs.nga.cn/read.php?tid=999)"
        )

    def test_op_body_cleaned(self) -> None:
        op = self.page.posts[0]
        body = text_of(op.blocks)
        self.assertIn("【[写作] [短篇] 测试帖】", body)
        self.assertIn("这是对二楼的回复。", body)
        # 原始 BBCode 与编辑标记都不应残留。
        self.assertNotIn("本帖最后由", body)
        self.assertNotIn("[pid=", body)
        self.assertNotIn("[img]", body)
        self.assertNotIn("[quote]", body)

    def test_missing_author_resolved_from_user_map(self) -> None:
        reply = self.page.posts[1]
        self.assertEqual(reply.lou, 1)
        self.assertEqual(reply.author, "二楼用户")
        self.assertEqual(reply.author_id, 10002)
        self.assertFalse(reply.is_op)
        self.assertEqual(reply.missing_fields, [MISSING_TIME])
        self.assertIsNone(reply.posted_at)

    def test_missing_author_without_user_map(self) -> None:
        reply = self.page.posts[2]
        self.assertEqual(reply.lou, 2)
        self.assertIsNone(reply.author)
        self.assertEqual(reply.author_id, 10003)  # authorid 存在，但 __U 无对应昵称
        self.assertFalse(reply.is_op)
        self.assertEqual(reply.missing_fields, [MISSING_AUTHOR])
        self.assertEqual(reply.posted_at, "2024-04-12 11:35")

    def test_missing_field_counts(self) -> None:
        self.assertEqual(self.page.missing_fields, {MISSING_TIME: 1, MISSING_AUTHOR: 1})

    def test_warnings_mention_missing_fields(self) -> None:
        joined = " ".join(self.page.warnings)
        self.assertIn("时间", joined)
        self.assertIn("作者", joined)

    def test_input_cache_not_mutated(self) -> None:
        before = json.dumps(self.cache, ensure_ascii=False, sort_keys=True)
        parse_cache(self.cache)
        after = json.dumps(self.cache, ensure_ascii=False, sort_keys=True)
        self.assertEqual(before, after)


class ParseCacheBehaviorTest(unittest.TestCase):
    """字段降级、时间戳、跳过逻辑与畸形数据。"""

    def setUp(self) -> None:
        self.cache = load_fixture("page_001.json")

    def test_timestamp_fallback_seconds(self) -> None:
        cache = copy.deepcopy(self.cache)
        reply = cache["content"]["data"]["__R"][1]
        reply["postdate"] = None
        reply["postdatetimestamp"] = 1712892600
        page = parse_cache(cache)
        self.assertEqual(page.posts[1].posted_at, "2024-04-12 11:30")

    def test_timestamp_fallback_milliseconds(self) -> None:
        cache = copy.deepcopy(self.cache)
        reply = cache["content"]["data"]["__R"][1]
        reply["postdate"] = None
        reply["postdatetimestamp"] = 1712892600000  # 明显是毫秒
        page = parse_cache(cache)
        self.assertEqual(page.posts[1].posted_at, "2024-04-12 11:30")

    def test_invalid_timestamp_marks_missing(self) -> None:
        cache = copy.deepcopy(self.cache)
        reply = cache["content"]["data"]["__R"][1]
        reply["postdate"] = ""
        reply["postdatetimestamp"] = "not-a-timestamp"
        page = parse_cache(cache)
        self.assertIsNone(page.posts[1].posted_at)
        self.assertIn(MISSING_TIME, page.posts[1].missing_fields)

    def test_postdate_takes_priority(self) -> None:
        cache = copy.deepcopy(self.cache)
        reply = cache["content"]["data"]["__R"][2]
        reply["postdate"] = "2024-01-01 00:00"
        reply["postdatetimestamp"] = 1712892900
        page = parse_cache(cache)
        self.assertEqual(page.posts[2].posted_at, "2024-01-01 00:00")

    def test_missing_content_marks_body(self) -> None:
        cache = copy.deepcopy(self.cache)
        cache["content"]["data"]["__R"][2]["content"] = ""
        page = parse_cache(cache)
        post = page.posts[2]
        self.assertEqual(post.blocks, [])
        self.assertIn(MISSING_BODY, post.missing_fields)

    def test_title_cleaning_strips_bbcode_and_html_only(self) -> None:
        cache = copy.deepcopy(self.cache)
        cache["content"]["data"]["__T"]["subject"] = (
            "[b]粗体[/b] <i>HTML</i> [color=red]颜色[/color] [写作] 标题\n换行"
        )
        page = parse_cache(cache)
        self.assertEqual(page.title, "粗体 HTML 颜色 [写作] 标题 换行")

    def test_login_wall_is_skipped(self) -> None:
        page = parse_cache(load_fixture("page_login_wall.json"))
        self.assertTrue(page.skipped)
        self.assertEqual(page.posts, [])
        self.assertEqual(page.status, "auth_error")
        self.assertEqual(page.page_type, "login_wall")
        self.assertTrue(page.skip_reason)
        self.assertIn("auth_error", page.skip_reason or "")
        self.assertIn("login_wall", page.skip_reason or "")

    def test_error_page_is_skipped(self) -> None:
        page = parse_cache(load_fixture("page_error.json"))
        self.assertTrue(page.skipped)
        self.assertEqual(page.posts, [])
        self.assertEqual(page.status, "error")
        self.assertEqual(page.page_type, "error")
        self.assertIn("error", page.skip_reason or "")

    def test_non_json_source_is_skipped(self) -> None:
        cache = copy.deepcopy(self.cache)
        cache["content"]["source"] = "html"
        page = parse_cache(cache)
        self.assertTrue(page.skipped)
        self.assertEqual(page.posts, [])
        reason = page.skip_reason or ""
        self.assertIn("content.source", reason)
        self.assertIn("html", reason)
        # 外层其它字段可用时仍保留在 ParsedPage 上，方便 engine 汇总。
        self.assertEqual(page.tid, 123)
        self.assertEqual(page.page, 1)
        self.assertEqual(page.total_pages, 2)

    def test_missing_data_is_skipped(self) -> None:
        cache = copy.deepcopy(self.cache)
        del cache["content"]["data"]
        page = parse_cache(cache)
        self.assertTrue(page.skipped)
        self.assertEqual(page.posts, [])
        self.assertIn("data", page.skip_reason or "")

    def test_non_dict_cache_does_not_crash(self) -> None:
        for value in (None, "", [], 123, "not-a-cache"):
            with self.subTest(value=value):
                page = parse_cache(value)  # type: ignore[arg-type]
                self.assertIsInstance(page, ParsedPage)
                self.assertTrue(page.skipped)
                self.assertEqual(page.posts, [])
                self.assertEqual(page.tid, 0)
                self.assertEqual(page.page, 0)
                self.assertTrue(page.skip_reason)

    def test_malformed_replies_and_users_do_not_crash(self) -> None:
        cache = {
            "tid": 1,
            "page": 1,
            "status": "ok",
            "page_type": "thread",
            "total_pages": 1,
            "fetcher_version": "1.0.0",
            "fetched_at": "2026-10-23T00:00:00+08:00",
            "req_id": None,
            "content": {
                "source": "json",
                "data": {
                    "__T": {"subject": 123, "authorid": "abc", "author": ["x"]},
                    "__R": [
                        None,
                        "oops",
                        {"lou": "bad", "pid": "bad", "authorid": "bad", "content": 123},
                        {
                            "lou": 1,
                            "pid": 2,
                            "authorid": 2,
                            "content": "正常正文",
                            "postdate": "2024-04-12 11:30",
                        },
                    ],
                    "__U": {"bad": "string-value", "2": {"nickname": "昵称二"}},
                },
            },
        }
        page = parse_cache(cache)
        self.assertIsInstance(page, ParsedPage)
        self.assertFalse(page.skipped)
        self.assertIsNone(page.title)
        self.assertIsNone(page.op_author)
        # 两条非 dict 回复被跳过，剩下两条 dict 继续解析。
        self.assertEqual(len(page.posts), 2)
        self.assertEqual(
            page.posts[0].missing_fields,
            [MISSING_LOU, MISSING_AUTHOR, MISSING_TIME, MISSING_BODY],
        )
        self.assertEqual(page.posts[1].author, "昵称二")
        self.assertEqual(page.posts[1].author_id, 2)
        self.assertEqual(page.posts[1].posted_at, "2024-04-12 11:30")
        self.assertTrue(page.warnings)

    def test_missing_replies_array_does_not_crash(self) -> None:
        cache = copy.deepcopy(self.cache)
        cache["content"]["data"]["__R"] = "不再是数组"
        page = parse_cache(cache)
        self.assertFalse(page.skipped)
        self.assertEqual(page.posts, [])
        self.assertTrue(any("__R" in warning for warning in page.warnings))


class ParseCacheMissingFieldsFixtureTest(unittest.TestCase):
    """page_missing_fields.json：业务字段尽可能缺失，验证占位与不崩溃。"""

    def setUp(self) -> None:
        self.page = parse_cache(load_fixture("page_missing_fields.json"))

    def test_page_level_missing(self) -> None:
        self.assertFalse(self.page.skipped)
        self.assertIsNone(self.page.title)
        self.assertIsNone(self.page.op_author)
        self.assertIsNone(self.page.total_pages)
        self.assertEqual(len(self.page.posts), 1)

    def test_post_level_missing(self) -> None:
        post = self.page.posts[0]
        self.assertIsNone(post.lou)
        self.assertIsNone(post.pid)
        self.assertIsNone(post.author)
        self.assertIsNone(post.author_id)
        self.assertIsNone(post.posted_at)
        self.assertEqual(post.blocks, [])
        self.assertIsNone(post.edited)
        self.assertFalse(post.is_op)
        self.assertEqual(
            post.missing_fields,
            [MISSING_LOU, MISSING_AUTHOR, MISSING_TIME, MISSING_BODY],
        )

    def test_missing_counts(self) -> None:
        self.assertEqual(
            self.page.missing_fields,
            {MISSING_LOU: 1, MISSING_AUTHOR: 1, MISSING_TIME: 1, MISSING_BODY: 1},
        )

    def test_warnings_cover_page_and_post_fields(self) -> None:
        joined = " ".join(self.page.warnings)
        for field in ("标题", "楼主", "总页数", "楼层", "作者", "时间", "正文"):
            with self.subTest(field=field):
                self.assertIn(field, joined)


class BbcodeTest(unittest.TestCase):
    """bbcode_to_blocks 的标签覆盖（方案附录 C / §6.10）。"""

    def test_empty_text(self) -> None:
        self.assertEqual(bbcode_to_blocks(""), ([], None, []))
        self.assertEqual(bbcode_to_blocks("   \n\t "), ([], None, []))
        self.assertEqual(bbcode_to_blocks(None), ([], None, []))  # type: ignore[arg-type]

    def test_style_tags_keep_text(self) -> None:
        blocks, edited, warnings = bbcode_to_blocks(
            "[b]粗体[/b][i]斜体[/i][u]下划线[/u][del]删除[/del]"
            "[color=red]颜色[/color][size=110%]字号[/size][font=微软雅黑]字体[/font]"
            "[align=center]居中[/align][collapse=标题]折叠[/collapse]"
        )
        self.assertEqual(text_of(blocks), "粗体斜体下划线删除颜色字号字体居中折叠")
        self.assertIsNone(edited)
        self.assertEqual(warnings, [])

    def test_chinese_brackets_preserved(self) -> None:
        blocks, _, _ = bbcode_to_blocks("[写作] [短篇] 正文")
        self.assertEqual(text_of(blocks), "[写作] [短篇] 正文")

    def test_unknown_ascii_tag_shell_removed(self) -> None:
        blocks, _, _ = bbcode_to_blocks("[foobar]内容[/foobar]")
        self.assertEqual(text_of(blocks), "内容")

    def test_nested_quote_structure(self) -> None:
        blocks, _, warnings = bbcode_to_blocks(
            "[quote]外层[quote]内层[/quote]外层二[/quote]"
        )
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].kind, "quote")
        children = blocks[0].children
        self.assertEqual([child.kind for child in children], ["text", "quote", "text"])
        self.assertEqual(children[0].text, "外层")
        self.assertEqual(text_of(children[1].children), "内层")
        self.assertEqual(children[2].text, "外层二")
        self.assertEqual(warnings, [])

    def test_unclosed_quote_kept_as_text(self) -> None:
        blocks, _, warnings = bbcode_to_blocks("[quote]没有闭合")
        self.assertIn("[quote]", text_of(blocks))
        self.assertTrue(warnings)

    def test_quote_depth_limit(self) -> None:
        depth = MAX_QUOTE_DEPTH + 5
        raw = "[quote]" * depth + "核心内容" + "[/quote]" * depth
        blocks, _, warnings = bbcode_to_blocks(raw)
        self.assertIn("核心内容", text_of(blocks))
        self.assertTrue(any(str(MAX_QUOTE_DEPTH) in warning for warning in warnings))

    def test_images_absolutized(self) -> None:
        cases = {
            "https://img.nga.cn/a.jpg": "https://img.nga.cn/a.jpg",
            "http://img.nga.cn/a.jpg": "http://img.nga.cn/a.jpg",
            "//img.nga.cn/a.jpg": "https://img.nga.cn/a.jpg",
            "./mon_202404/12/a.jpg": (
                "https://img.nga.cn/attachments/mon_202404/12/a.jpg"
            ),
            "mon_202404/12/a.jpg": (
                "https://img.nga.cn/attachments/mon_202404/12/a.jpg"
            ),
            "/attachments/mon_1.jpg": "https://bbs.nga.cn/attachments/mon_1.jpg",
        }
        for raw_url, expected in cases.items():
            with self.subTest(raw=raw_url):
                blocks, _, _ = bbcode_to_blocks("[img]" + raw_url + "[/img]")
                self.assertEqual(len(blocks), 1)
                self.assertEqual(blocks[0].kind, "image")
                self.assertEqual(blocks[0].text, expected)

    def test_links(self) -> None:
        blocks, _, _ = bbcode_to_blocks(
            "[url=https://bbs.nga.cn/read.php?tid=1]看这里[/url]"
        )
        self.assertEqual(blocks[0].kind, "link")
        self.assertEqual(
            blocks[0].text, "看这里 (https://bbs.nga.cn/read.php?tid=1)"
        )

        blocks, _, _ = bbcode_to_blocks("[url]https://example.com[/url]")
        self.assertEqual(blocks[0].kind, "link")
        self.assertEqual(blocks[0].text, "https://example.com")

        blocks, _, _ = bbcode_to_blocks("[url=https://example.com]https://example.com[/url]")
        self.assertEqual(blocks[0].text, "https://example.com")

        blocks, _, _ = bbcode_to_blocks("[url]/read.php?tid=123[/url]")
        self.assertEqual(blocks[0].text, "https://bbs.nga.cn/read.php?tid=123")

    def test_pid_uid_shell_removed(self) -> None:
        blocks, _, _ = bbcode_to_blocks("[pid=1,2]回复文本[/pid] [uid=9]用户甲[/uid]")
        self.assertEqual(text_of(blocks), "回复文本 用户甲")

    def test_emote_replaced(self) -> None:
        blocks, _, _ = bbcode_to_blocks("前面[s:1:微笑]后面")
        self.assertEqual(text_of(blocks), "前面[表情]后面")

    def test_list_item_marker(self) -> None:
        blocks, _, _ = bbcode_to_blocks("[list][*]一[*]二[/list]")
        self.assertIn("- 一", text_of(blocks))
        self.assertIn("- 二", text_of(blocks))

    def test_code_keeps_inner_text(self) -> None:
        blocks, _, _ = bbcode_to_blocks("[code]x = 1\ny = 2[/code]")
        self.assertIn("x = 1\ny = 2", text_of(blocks))

    def test_html_entities_unescaped(self) -> None:
        blocks, _, _ = bbcode_to_blocks("&amp; &lt; &gt; &quot;")
        self.assertEqual(text_of(blocks), "& < > \"")

    def test_edit_notice_extracted_and_removed(self) -> None:
        blocks, edited, warnings = bbcode_to_blocks(
            "第一段\n\n本帖最后由 某某 于 2024-04-12 11:30 编辑"
        )
        self.assertEqual(edited, "本帖最后由 某某 于 2024-04-12 11:30 编辑")
        self.assertNotIn("本帖最后由", text_of(blocks))
        self.assertEqual(text_of(blocks).strip(), "第一段")
        self.assertEqual(warnings, [])

    def test_edit_notice_with_inline_tags(self) -> None:
        blocks, edited, _ = bbcode_to_blocks(
            "正文 [b]本帖最后由[/b] 某某 于 2024-04-12 11:30 编辑"
        )
        self.assertEqual(edited, "本帖最后由 某某 于 2024-04-12 11:30 编辑")
        self.assertNotIn("本帖最后由", text_of(blocks))

    def test_edit_notice_inside_quote_not_extracted(self) -> None:
        blocks, edited, _ = bbcode_to_blocks(
            "[quote]本帖最后由 甲 于 2024-04-12 11:30 编辑[/quote]正文"
        )
        self.assertIsNone(edited)
        self.assertIn("本帖最后由", text_of(blocks))

    def test_absolutize_url_rules(self) -> None:
        self.assertEqual(absolutize_url("https://a/b"), "https://a/b")
        self.assertEqual(absolutize_url("//img.nga.cn/a.jpg"), "https://img.nga.cn/a.jpg")
        self.assertEqual(
            absolutize_url("./mon_x.jpg"),
            "https://img.nga.cn/attachments/mon_x.jpg",
        )
        self.assertEqual(
            absolutize_url("/read.php?tid=1"),
            "https://bbs.nga.cn/read.php?tid=1",
        )
        self.assertEqual(
            absolutize_url("read.php?tid=1"),
            "https://bbs.nga.cn/read.php?tid=1",
        )
        self.assertEqual(absolutize_url(None), "")
        self.assertEqual(absolutize_url("  "), "")


class FixtureHygieneTest(unittest.TestCase):
    """fixtures 必须是缓存外层 schema 且不含任何凭据/个人信息。"""

    def test_fixtures_exist_and_follow_outer_schema(self) -> None:
        for name in FIXTURE_NAMES:
            with self.subTest(name=name):
                record = load_fixture(name)
                self.assertIsInstance(record, dict)
                self.assertIn("content", record)
                self.assertIsInstance(record["content"], dict)
                self.assertIn("source", record["content"])

    def test_page_001_declares_required_metadata(self) -> None:
        record = load_fixture("page_001.json")
        self.assertEqual(record["tid"], 123)
        self.assertEqual(record["page"], 1)
        self.assertEqual(record["status"], "ok")
        self.assertEqual(record["page_type"], "thread")
        self.assertEqual(record["total_pages"], 2)

    def test_no_credentials_in_fixtures(self) -> None:
        for name in FIXTURE_NAMES:
            with self.subTest(name=name):
                raw = (FIXTURES / name).read_text(encoding="utf-8").lower()
                for forbidden in ("cookie", "set-cookie", "nga_uid", "nga_cid", "authorization"):
                    self.assertNotIn(forbidden, raw)


if __name__ == "__main__":
    unittest.main()
