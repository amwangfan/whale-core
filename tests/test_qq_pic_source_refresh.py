"""QQ 图片原始下载源保留 + rkey 刷新后重建的离线测试（2026-11-11 13:00 gap 修复）。

复现生产 gap：SSE 入站（13:00:13）早于本机有效 rkey 写盘（13:10:57），
附件 url/urls 为空且 originalUrl/fileToken 被丢弃，rkey 刷新后也无法恢复。
修复后：附件保留原始来源（original_url/file_token/chat_type），
resolve_pic_attachment_urls(att) 每次调用现读活 rkey 并复用 _pic_download_urls，
可在 rkey 刷新（WeQ refreshRkeys）后就地重建下载链——不重放 SSE 事件、
不另造下载器；下载与 SSRF/格式/大小校验仍由 core media validator 负责。

不起真实 WeQ、不连 QQ、不联网、不用真实凭据：rkey/fileToken 全部合成。
"""
import json
import time
from urllib.parse import quote

from weq_channel.wq_push import (
    _load_weq_rkeys,
    _pic_download_urls,
    resolve_pic_attachment_urls,
    sse_message_to_incoming,
)

SELF_UIN = "1900000001"   # 纯合成测试号（不对应任何真实账号）
OWNER_UIN = "10001"
REL_URL = "/download?appid=1406&fileid=TOK&spec=0"
ABS_QQ = "https://multimedia.nt.qq.com.cn"


def write_rkeys(tmp_path, entries):
    account_dir = tmp_path / "config" / "accounts"
    account_dir.mkdir(parents=True, exist_ok=True)
    (account_dir / "acc.json").write_text(json.dumps({"nested": {"rkeys": entries}}))


def fresh_rk(value, type_):
    return {"rkey": value, "type": type_,
            "createTime": time.time(), "ttlSeconds": 3600}


def live_rkeys(tmp_path):
    """写入 c2c(type10)+群(type20) 各一条活 rkey，返回 _load_weq_rkeys 结果。"""
    write_rkeys(tmp_path, [fresh_rk("&rkey=LIVE", 10), fresh_rk("&rkey=GRP", 20)])
    return _load_weq_rkeys(str(tmp_path))


def pic_event(pic_data, *, chat_type="c2c", msg_id="m1"):
    return {
        "type": "message",
        "chatType": chat_type,
        "convId": OWNER_UIN if chat_type == "c2c" else "30003",
        "msgId": msg_id,
        "senderUid": "u_" + OWNER_UIN,
        "senderUin": OWNER_UIN,
        "sendTime": "1793280000",
        "text": "",
        "elements": [{"type": "pic", "data": pic_data}],
    }


def test_rkey_gap_keeps_raw_source_and_helper_rebuilds(tmp_path, monkeypatch):
    """13:00 gap 复现：SSE 时无活 rkey → url/urls 空（诚实不伪造），
    但原始来源保留；rkey 刷新落盘后 helper 就地重建下载链。"""
    monkeypatch.setenv("WHALE_WEQ_CONFIG_DIR", str(tmp_path))
    data = {"originalUrl": REL_URL, "fileToken": "TOK",
            "md5": "d41d8cd98f00b204e9800998ecf8427e", "filePath": ""}
    incoming = sse_message_to_incoming(pic_event(data), self_uin=SELF_UIN)
    att = incoming.attachments[0]
    assert att["url"] == "" and att["urls"] == ()      # rkey 空窗：无候选链
    assert att["original_url"] == REL_URL              # 原始来源保留
    assert att["file_token"] == "TOK"
    assert att["chat_type"] == "c2c"
    assert att["md5"] == "d41d8cd98f00b204e9800998ecf8427e"
    assert resolve_pic_attachment_urls(att) == ()      # 仍无 rkey → 诚实为空

    live_rkeys(tmp_path)                               # 模拟新 rkey 签发并写盘
    urls = resolve_pic_attachment_urls(att)
    assert urls[0] == ABS_QQ + REL_URL + "&rkey=LIVE"  # c2c 场景 appid1406 优先
    assert any(u.endswith("&rkey=GRP") and "appid=1407" in u for u in urls)


def test_file_token_only_signed_chars_percent_encoded(tmp_path):
    """fileToken-only 拼 URL 必须 quote：签名 token 含 + / = 时裸拼会损坏
    查询串；c2c→appid=1406+type10 rkey 优先，group→appid=1407+type20 优先。"""
    rkeys = live_rkeys(tmp_path)
    token = "a+b/c==d"
    enc = quote(token, safe="")
    assert enc == "a%2Bb%2Fc%3D%3Dd"

    urls = _pic_download_urls({"fileToken": token}, chat_type="c2c", rkeys=rkeys)
    assert urls[0] == f"{ABS_QQ}/download?appid=1406&fileid={enc}&spec=0&rkey=LIVE"

    gurls = _pic_download_urls({"fileToken": token}, chat_type="group", rkeys=rkeys)
    assert gurls[0] == f"{ABS_QQ}/download?appid=1407&fileid={enc}&spec=0&rkey=GRP"
    fileid = gurls[0].split("fileid=")[1].split("&")[0]
    assert "+" not in fileid and "/" not in fileid and "=" not in fileid


def test_helper_token_only_group_scene(tmp_path, monkeypatch):
    """helper 走附件 chat_type：群附件在 rkey 更新后按群场景重建。"""
    monkeypatch.setenv("WHALE_WEQ_CONFIG_DIR", str(tmp_path))
    live_rkeys(tmp_path)
    att = {"kind": "pic", "file_path": "", "url": "", "urls": (),
           "original_url": "", "file_token": "TOK", "chat_type": "group"}
    urls = resolve_pic_attachment_urls(att)
    assert urls[0].endswith("&rkey=GRP") and "appid=1407" in urls[0]


def test_group_event_attachment_keeps_chat_type(tmp_path, monkeypatch):
    """群事件（accept_group）：附件保留 chat_type=group，SSE 时即按群场景建链，
    helper 重读结果一致（幂等）。"""
    monkeypatch.setenv("WHALE_WEQ_CONFIG_DIR", str(tmp_path))
    live_rkeys(tmp_path)
    incoming = sse_message_to_incoming(
        pic_event({"originalUrl": REL_URL}, chat_type="group"),
        self_uin=SELF_UIN, accept_group=True)
    att = incoming.attachments[0]
    assert att["chat_type"] == "group"
    assert att["url"].endswith("&rkey=GRP") and "appid=1407" in att["url"]
    assert resolve_pic_attachment_urls(att) == tuple(att["urls"])


def test_absolute_non_qq_url_never_gets_rkey(tmp_path, monkeypatch):
    """非 QQ 域的绝对 URL：原样保留（不追加 rkey、不再编码）。"""
    monkeypatch.setenv("WHALE_WEQ_CONFIG_DIR", str(tmp_path))
    live_rkeys(tmp_path)
    data = {"originalUrl": "https://cdn.example.com/x.png?a=1&b=+"}
    urls = _pic_download_urls(data, chat_type="c2c",
                              rkeys=_load_weq_rkeys(str(tmp_path)))
    assert urls == ("https://cdn.example.com/x.png?a=1&b=+",)
    att = {"kind": "pic", "original_url": data["originalUrl"],
           "file_token": "", "chat_type": "c2c"}
    assert resolve_pic_attachment_urls(att) == urls


def test_absolute_qq_download_rebuilt_with_live_rkeys(tmp_path):
    """绝对 QQ /download 地址：缺 rkey 补上、旧 rkey 原位替换——
    用当前活 rkey 重建候选；原地址保留为首个候选，rkey 不重复。"""
    rkeys = live_rkeys(tmp_path)
    # 旧 rkey → 原位替换为活值，恰出现一次
    stale = ABS_QQ + "/download?appid=1406&fileid=TOK&spec=0&rkey=OLD"
    urls = _pic_download_urls({"originalUrl": stale}, chat_type="c2c", rkeys=rkeys)
    assert urls[0] == stale                      # 原始地址仍是首个候选
    rebuilt = urls[1]
    assert rebuilt.count("rkey=") == 1
    assert "rkey=LIVE" in rebuilt and "rkey=OLD" not in rebuilt
    assert rebuilt.endswith("spec=0&rkey=LIVE")  # 原位替换，参数位置不变
    assert any(u.endswith("spec=0&rkey=GRP") and "appid=1407" in u for u in urls)
    # 缺 rkey → 直接追加
    bare = ABS_QQ + "/download?appid=1406&fileid=TOK&spec=0"
    urls2 = _pic_download_urls({"originalUrl": bare}, chat_type="c2c", rkeys=rkeys)
    assert urls2[0] == bare
    assert urls2[1] == bare + "&rkey=LIVE"
    assert all(u.count("rkey=") <= 1 for u in urls2)


def test_relative_url_embedded_rkey_replaced_not_duplicated(tmp_path):
    """相对 /download 已含（旧）rkey：原位替换为活值，不重复追加。"""
    rkeys = live_rkeys(tmp_path)
    data = {"originalUrl": "/download?appid=1406&fileid=TOK&spec=0&rkey=OLD"}
    urls = _pic_download_urls(data, chat_type="c2c", rkeys=rkeys)
    assert urls[0] == ABS_QQ + "/download?appid=1406&fileid=TOK&spec=0&rkey=LIVE"
    assert urls[0].count("rkey=") == 1 and "OLD" not in urls[0]


def test_helper_without_raw_source_is_honest_empty(tmp_path, monkeypatch):
    """无原始来源（旧结构附件/非 pic/None）→ 空 tuple，绝不伪造 URL。"""
    monkeypatch.setenv("WHALE_WEQ_CONFIG_DIR", str(tmp_path))
    live_rkeys(tmp_path)
    assert resolve_pic_attachment_urls({"kind": "pic"}) == ()
    assert resolve_pic_attachment_urls(
        {"kind": "pic", "original_url": "", "file_token": ""}) == ()
    assert resolve_pic_attachment_urls(None) == ()


def test_debug_log_does_not_leak_file_token(tmp_path, monkeypatch, capsys):
    """fileToken 是签名凭据：调试输出只允许字段名，值不得出现在任何日志。"""
    monkeypatch.setenv("WHALE_WEQ_CONFIG_DIR", str(tmp_path))
    secret = "SECRET+TOKEN/VALUE=="
    incoming = sse_message_to_incoming(
        pic_event({"originalUrl": REL_URL, "fileToken": secret}),
        self_uin=SELF_UIN)
    captured = capsys.readouterr().out
    assert secret not in captured
    assert quote(secret, safe="") not in captured
    assert "rkey=" not in captured
    # 结构内保留（供 helper 重建），日志零泄漏
    assert incoming.attachments[0]["file_token"] == secret
