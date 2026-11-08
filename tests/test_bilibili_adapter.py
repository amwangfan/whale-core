from whale_core.life.bilibili import BilibiliContentAdapter


def test_bilibili_adapter_only_accepts_public_video_pages_and_fetches_them():
    calls = []

    def search(query, *, limit):
        calls.append(("search", query, limit))
        return (
            "FPGA 入门 — 一些介绍（https://www.bilibili.com/video/BV1Abc123/）\n"
            "Not Bilibili — ignore (https://example.com/video/BV2abc456/)\n"
            "short link — ignore (https://b23.tv/abcd)"
        )

    def fetch(url):
        calls.append(("fetch", url))
        return "视频页面简介：介绍 FPGA 的基本概念与开发板实验。"

    adapter = BilibiliContentAdapter(search, fetch)
    items = adapter.discover("FPGA")

    assert len(items) == 1
    assert items[0].item_id == "BV1Abc123"
    assert items[0].source_name == "哔哩哔哩"
    assert items[0].content_type == "video"
    assert items[0].inspected is True
    assert items[0].inspection_method == "public_page_description"
    assert "FPGA" in items[0].summary
    assert calls[0][1].startswith("site:bilibili.com/video")
    assert calls[1][1] == "https://www.bilibili.com/video/BV1Abc123/"


def test_bilibili_adapter_does_not_mark_empty_fetch_as_inspected():
    adapter = BilibiliContentAdapter(
        lambda query, *, limit: "title — snippet（https://www.bilibili.com/video/BV1Abc123/）",
        lambda url: "",
    )
    assert adapter.discover("topic") == []
