"""render.py 单元测试：纯字符串排版，离线、确定性，不写任何文件。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from parser import Block, ParsedPost, parse_cache
from render import (
    HEADER_SEPARATOR,
    PLACEHOLDER_AUTHOR,
    PLACEHOLDER_BODY,
    PLACEHOLDER_FETCHED,
    PLACEHOLDER_LOU,
    PLACEHOLDER_OP_AUTHOR,
    PLACEHOLDER_TITLE,
    PLACEHOLDER_TIME,
    PLACEHOLDER_TOTAL_PAGES,
    PLACEHOLDER_VERSION,
    POST_SEPARATOR,
    OutputMeta,
    render_header,
    render_post,
    render_txt,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def make_meta(**overrides) -> OutputMeta:
    options = dict(
        tid=123,
        title="[写作] 测试帖",
        op_author="楼主先生",
        total_pages=2,
        page_range="1",
        fetched_min="2026-10-23 12:34:56",
        fetched_max="2026-10-23 12:40:00",
        parser_version="1.0.0",
        fetcher_version="1.0.0",
        post_count=3,
        missing_count=0,
    )
    options.update(overrides)
    return OutputMeta(**options)


def make_post(**overrides) -> ParsedPost:
    options = dict(
        lou=0,
        pid=900000001,
        author="楼主先生",
        author_id=10001,
        posted_at="2024-04-12 11:27",
        blocks=[Block("text", "正文第一段")],
        edited=None,
        is_op=True,
        missing_fields=[],
    )
    options.update(overrides)
    return ParsedPost(**options)


class RenderHeaderTest(unittest.TestCase):
    def test_separator_and_core_fields(self) -> None:
        header = render_header(make_meta())
        lines = header.split("\n")
        self.assertEqual(lines[0], "=" * 80)
        self.assertEqual(lines[-1], "=" * 80)
        self.assertEqual(header.count(HEADER_SEPARATOR), 2)
        self.assertFalse(header.endswith("\n"))
        self.assertIn("NGA 帖子导出", header)
        self.assertIn("TID: 123", header)
        self.assertIn("标题: [写作] 测试帖", header)
        self.assertIn("楼主: 楼主先生", header)
        self.assertIn("总页数: 2    本次收录页: 1", header)
        self.assertIn(
            "抓取时间: 2026-10-23 12:34:56 ~ 2026-10-23 12:40:00", header
        )
        self.assertIn(
            "parser_version: 1.0.0    fetcher_version: 1.0.0", header
        )
        self.assertIn("收录楼层: 3    字段缺失: 0 处", header)

    def test_page_range_multi(self) -> None:
        header = render_header(make_meta(page_range="1-20", total_pages=20))
        self.assertIn("总页数: 20    本次收录页: 1-20", header)

    def test_fetched_single_instant_keeps_range_format(self) -> None:
        # engine 单页时 min == max，模板仍是 "min ~ max"。
        header = render_header(
            make_meta(
                fetched_min="2026-10-23 12:00:00",
                fetched_max="2026-10-23 12:00:00",
            )
        )
        self.assertIn(
            "抓取时间: 2026-10-23 12:00:00 ~ 2026-10-23 12:00:00", header
        )

    def test_fetched_iso8601_converted_to_utc8(self) -> None:
        header = render_header(
            make_meta(
                fetched_min="2026-10-23T12:34:56+08:00",
                fetched_max="2026-10-23T12:40:00+08:00",
            )
        )
        self.assertIn(
            "抓取时间: 2026-10-23 12:34:56 ~ 2026-10-23 12:40:00", header
        )

    def test_missing_placeholders_exact(self) -> None:
        header = render_header(
            make_meta(
                title=None,
                op_author=None,
                total_pages=None,
                fetched_min=None,
                fetched_max=None,
                fetcher_version=None,
            )
        )
        self.assertIn("标题: " + PLACEHOLDER_TITLE, header)
        self.assertIn("楼主: " + PLACEHOLDER_OP_AUTHOR, header)
        self.assertIn("总页数: " + PLACEHOLDER_TOTAL_PAGES, header)
        self.assertIn("抓取时间: " + PLACEHOLDER_FETCHED, header)
        self.assertIn("fetcher_version: " + PLACEHOLDER_VERSION, header)

    def test_empty_string_fields_use_placeholders(self) -> None:
        header = render_header(make_meta(title="   ", op_author=""))
        self.assertIn("标题: " + PLACEHOLDER_TITLE, header)
        self.assertIn("楼主: " + PLACEHOLDER_OP_AUTHOR, header)


class RenderPostTest(unittest.TestCase):
    def test_floor_separator_and_fields(self) -> None:
        out = render_post(make_post())
        lines = out.split("\n")
        self.assertEqual(
            lines[0], f"{POST_SEPARATOR} 第 0 楼 | 楼主先生 | 2024-04-12 11:27 {POST_SEPARATOR}"
        )
        self.assertEqual(lines[1], "作者: 楼主先生（楼主）")
        self.assertEqual(lines[2], "时间: 2024-04-12 11:27")
        self.assertEqual(lines[3], "")
        self.assertEqual(lines[4], "正文:")
        self.assertEqual(lines[5], "正文第一段")
        self.assertFalse(out.endswith("\n"))

    def test_non_op_author_line(self) -> None:
        out = render_post(
            make_post(lou=1, author="二楼用户", author_id=10002, is_op=False)
        )
        self.assertIn("作者: 二楼用户", out)
        self.assertNotIn("（楼主）", out)

    def test_missing_placeholders_exact(self) -> None:
        out = render_post(
            make_post(lou=None, author=None, posted_at=None, blocks=[], is_op=False)
        )
        lines = out.split("\n")
        self.assertEqual(
            lines[0],
            f"{POST_SEPARATOR} 第 {PLACEHOLDER_LOU} 楼 | {PLACEHOLDER_AUTHOR}"
            f" | {PLACEHOLDER_TIME} {POST_SEPARATOR}",
        )
        self.assertEqual(lines[1], "作者: " + PLACEHOLDER_AUTHOR)
        self.assertEqual(lines[2], "时间: " + PLACEHOLDER_TIME)
        body_index = lines.index("正文:")
        self.assertEqual(lines[body_index + 1], PLACEHOLDER_BODY)

    def test_quote_prefix_per_line(self) -> None:
        post = make_post(
            blocks=[
                Block("quote", "", [Block("text", "引用第一行\n引用第二行")]),
            ]
        )
        out = render_post(post)
        self.assertIn("\n> 引用第一行\n> 引用第二行", out)

    def test_nested_quote_accumulates_prefix(self) -> None:
        post = make_post(
            blocks=[
                Block(
                    "quote",
                    "",
                    [
                        Block("text", "外层"),
                        Block("quote", "", [Block("text", "内层")]),
                    ],
                )
            ]
        )
        out = render_post(post)
        self.assertIn("> 外层", out)
        self.assertIn("> > 内层", out)

    def test_image_on_its_own_line(self) -> None:
        out = render_post(
            make_post(
                blocks=[
                    Block("text", "前文"),
                    Block("image", "https://img.nga.cn/attachments/a.jpg"),
                    Block("text", "后文"),
                ]
            )
        )
        self.assertIn("\n[图片: https://img.nga.cn/attachments/a.jpg]\n", out)
        image_line = "[图片: https://img.nga.cn/attachments/a.jpg]"
        self.assertIn(image_line, out.split("\n"))

    def test_link_rendered_inline(self) -> None:
        out = render_post(
            make_post(
                blocks=[
                    Block("text", "请看 "),
                    Block("link", "站内链接 (https://bbs.nga.cn/read.php?tid=1)"),
                    Block("text", " 完"),
                ]
            )
        )
        self.assertIn(
            "请看 站内链接 (https://bbs.nga.cn/read.php?tid=1) 完", out
        )

    def test_edited_line_after_body(self) -> None:
        edited = "本帖最后由 楼主先生 于 2024-04-12 11:30 编辑"
        out = render_post(make_post(blocks=[Block("text", "正文第一段")], edited=edited))
        self.assertIn("正文第一段\n\n[编辑] " + edited, out)
        self.assertGreater(out.index("[编辑]"), out.index("正文第一段"))
        self.assertEqual(out.split("\n")[-1], "[编辑] " + edited)

    def test_no_edited_line_when_empty(self) -> None:
        out = render_post(make_post(edited=None))
        self.assertNotIn("[编辑]", out)
        out = render_post(make_post(edited="   "))
        self.assertNotIn("[编辑]", out)

    def test_empty_body_placeholder(self) -> None:
        out = render_post(make_post(blocks=[]))
        self.assertIn("正文:\n" + PLACEHOLDER_BODY, out)

    def test_paragraph_whitespace_normalized(self) -> None:
        out = render_post(
            make_post(blocks=[Block("text", "第一段  \n\n\n\n第二段  ")])
        )
        self.assertIn("正文:\n第一段\n\n第二段", out)
        self.assertNotIn("第一段  ", out)
        self.assertNotIn("\n\n\n", out)

    def test_no_req_id_or_pid(self) -> None:
        post = make_post(pid=987654321, author_id=12345678)
        out = render_post(post)
        self.assertNotIn("987654321", out)
        self.assertNotIn("12345678", out)
        self.assertNotIn("pid", out)


class RenderTxtTest(unittest.TestCase):
    def test_layout(self) -> None:
        posts = [
            make_post(),
            make_post(
                lou=1,
                pid=900000002,
                author="二楼用户",
                author_id=10002,
                is_op=False,
                posted_at="2024-04-12 11:30",
                blocks=[Block("text", "二楼正文")],
            ),
        ]
        meta = make_meta(post_count=len(posts))
        text = render_txt(posts, meta)
        self.assertTrue(text.startswith(HEADER_SEPARATOR + "\n"))
        self.assertTrue(text.endswith("\n"))
        self.assertFalse(text.endswith("\n\n"))
        self.assertNotIn("\n\n\n", text)
        self.assertEqual(text.split("\n\n")[0], render_header(meta))
        self.assertEqual(text.count("===== 第 "), 2)
        self.assertIn("\n\n===== 第 1 楼", text)
        self.assertNotIn("req_id", text)

    def test_empty_items(self) -> None:
        meta = make_meta(post_count=0)
        self.assertEqual(render_txt([], meta), render_header(meta) + "\n")

    def test_fixture_end_to_end(self) -> None:
        record = json.loads(
            (FIXTURES / "page_001.json").read_text(encoding="utf-8")
        )
        page = parse_cache(record)
        self.assertFalse(page.skipped)
        meta = make_meta(
            tid=page.tid,
            title=page.title,
            op_author=page.op_author,
            total_pages=page.total_pages,
            page_range="1",
            fetched_min="2026-10-23 12:34:56",
            fetched_max="2026-10-23 12:34:56",
            fetcher_version=page.fetcher_version,
            post_count=len(page.posts),
            missing_count=sum(page.missing_fields.values()),
        )
        text = render_txt(page.posts, meta)

        self.assertEqual(text.count("===== 第 "), 3)
        self.assertIn("===== 第 0 楼 | 楼主先生 | 2024-04-12 11:27 =====", text)
        self.assertIn("作者: 楼主先生（楼主）", text)
        self.assertIn("> 引用内容第一行", text)
        self.assertIn(
            "[图片: https://img.nga.cn/attachments/mon_202404/12/abc.jpg]", text
        )
        self.assertIn("[编辑] 本帖最后由 楼主先生 于 2024-04-12 11:30 编辑", text)
        self.assertIn("===== 第 1 楼 | 二楼用户 | [缺失: 时间] =====", text)
        self.assertIn("===== 第 2 楼 | [缺失: 作者] | 2024-04-12 11:35 =====", text)
        self.assertIn("字段缺失: 2 处", text)
        self.assertIn("parser_version: 1.0.0", text)
        # 不输出 req_id / pid 与原始 BBCode。
        self.assertNotIn("123-1-abc123", text)
        for pid in ("900000001", "900000002", "900000003"):
            self.assertNotIn(pid, text)
        self.assertNotIn("req_id", text)
        self.assertNotIn("[quote]", text)
        self.assertNotIn("[img]", text)
        self.assertNotIn("[/url]", text)

    def test_engine_streaming_equivalent(self) -> None:
        """engine 流式写法（header + "\\n\\n" + 逐块 \\n\\n + "\\n"）与 render_txt 等价。"""
        posts = [make_post(), make_post(lou=1, author="二楼用户", is_op=False)]
        meta = make_meta(post_count=2)
        streamed = render_header(meta) + "\n\n"
        streamed += "\n\n".join(render_post(post) for post in posts)
        streamed += "\n"
        self.assertEqual(render_txt(posts, meta), streamed)


if __name__ == "__main__":
    unittest.main()
