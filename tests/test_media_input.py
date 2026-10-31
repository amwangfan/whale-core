"""媒体输入可信边界测试（G0-B，全部离线）。

覆盖：本地路径白名单（fail-closed、符号链接逃逸、.. 穿越、只读/未授权）、
内容嗅探（PNG/JPEG/GIF/WebP/BMP、格式伪装、尺寸/像素炸弹）、URL 下载
（https-only、SSRF 门控、逐跳重校验的重定向、体积护栏）。网络层全部走
注入的 fake opener / fake resolver，不触网。
"""

from __future__ import annotations

import io
import os
import socket
import struct
import urllib.error

import pytest

from whale_core.core.media_input import (
    ImageInputValidator,
    MediaInputError,
    sniff_image,
)


# ---------- 最小合法图片构造 ----------


def make_png(width: int = 8, height: int = 6) -> bytes:
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13) + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x06\x00\x00\x00" + b"\x00" * 16
    )


def make_gif(width: int = 7, height: int = 5) -> bytes:
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00" * 20


def make_bmp(width: int = 4, height: int = 3) -> bytes:
    return b"BM" + b"\x00" * 16 + struct.pack("<ii", width, height) + b"\x00" * 20


def make_jpeg(width: int = 9, height: int = 4) -> bytes:
    sof = b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", height, width) + b"\x03" + b"\x00" * 8
    return b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9 + sof + b"\xff\xd9"


def make_webp_vp8x(width: int = 11, height: int = 6) -> bytes:
    return (
        b"RIFF" + b"\x00" * 4 + b"WEBP" + b"VP8X" + b"\x00" * 8
        + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
        + b"\x00" * 8
    )


PUBLIC_IP = "93.184.216.34"


def public_resolver(host, port):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))]


# ---------- 内容嗅探 ----------


def test_sniff_all_supported_formats():
    assert sniff_image(make_png(8, 6)) == ("png", 8, 6)
    assert sniff_image(make_jpeg(9, 4)) == ("jpeg", 9, 4)
    assert sniff_image(make_gif(7, 5)) == ("gif", 7, 5)
    assert sniff_image(make_bmp(4, 3)) == ("bmp", 4, 3)
    assert sniff_image(make_webp_vp8x(11, 6)) == ("webp", 11, 6)


def test_sniff_rejects_non_image_and_corrupt():
    with pytest.raises(MediaInputError):
        sniff_image(b"not an image at all")
    with pytest.raises(MediaInputError):
        sniff_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4)  # 截断 PNG
    with pytest.raises(MediaInputError):
        sniff_image(b"")


# ---------- 本地路径白名单 ----------


@pytest.fixture()
def media_tree(tmp_path):
    root = tmp_path / "qq_cache"
    root.mkdir()
    (root / "pic.png").write_bytes(make_png())
    outside = tmp_path / "secret.png"
    outside.write_bytes(make_png())
    # 白名单内的符号链接 → 指向白名单外（逃逸）
    (root / "escape.png").symlink_to(outside)
    return {"root": root, "outside": outside}


def test_local_path_within_root_ok(media_tree):
    v = ImageInputValidator(allowed_roots=(str(media_tree["root"]),))
    img = v.from_local_path(str(media_tree["root"] / "pic.png"))
    assert img.mime == "image/png" and img.width == 8 and img.origin == "local"


def test_local_path_outside_root_rejected(media_tree):
    v = ImageInputValidator(allowed_roots=(str(media_tree["root"]),))
    with pytest.raises(MediaInputError, match="outside allowed roots"):
        v.from_local_path(str(media_tree["outside"]))


def test_local_path_symlink_escape_rejected(media_tree):
    v = ImageInputValidator(allowed_roots=(str(media_tree["root"]),))
    with pytest.raises(MediaInputError, match="outside allowed roots"):
        v.from_local_path(str(media_tree["root"] / "escape.png"))


def test_local_path_dotdot_traversal_rejected(media_tree):
    v = ImageInputValidator(allowed_roots=(str(media_tree["root"]),))
    evil = str(media_tree["root"] / ".." / "secret.png")
    with pytest.raises(MediaInputError, match="outside allowed roots"):
        v.from_local_path(evil)


def test_local_path_no_roots_fail_closed(media_tree):
    """未配置白名单 → 拒绝一切本地读取（fail-closed）。"""
    v = ImageInputValidator()
    with pytest.raises(MediaInputError, match="no allowed media roots"):
        v.from_local_path(str(media_tree["root"] / "pic.png"))


def test_local_path_directory_and_missing_rejected(media_tree):
    v = ImageInputValidator(allowed_roots=(str(media_tree["root"]),))
    with pytest.raises(MediaInputError):
        v.from_local_path(str(media_tree["root"]))  # 目录不是普通文件
    with pytest.raises(MediaInputError):
        v.from_local_path(str(media_tree["root"] / "nope.png"))


def test_local_path_unreadable_host_path(media_tree, monkeypatch):
    """只读宿主/未授权路径：权限错误 → MediaInputError，不崩溃不泄露。

    （测试以 root 运行时 chmod 0 不拦截读取，故用 monkeypatch 模拟 EACCES。）
    """
    import builtins

    target = media_tree["root"] / "denied.png"
    target.write_bytes(make_png())
    real_open = builtins.open

    def denying_open(path, *args, **kwargs):
        if str(path).endswith("denied.png"):
            raise PermissionError(13, "Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", denying_open)
    v = ImageInputValidator(allowed_roots=(str(media_tree["root"]),))
    with pytest.raises(MediaInputError, match="unreadable"):
        v.from_local_path(str(target))


def test_local_oversize_rejected(media_tree):
    v = ImageInputValidator(allowed_roots=(str(media_tree["root"]),), max_bytes=16)
    with pytest.raises(MediaInputError, match="size limit"):
        v.from_local_path(str(media_tree["root"] / "pic.png"))


def test_local_disguised_extension_still_validated(media_tree):
    """扩展名/MIME 不可信：.png 里的文本被拒，.txt 里的真 PNG 放行。"""
    fake = media_tree["root"] / "evil.png"
    fake.write_bytes(b"<html>not a picture</html>")
    real = media_tree["root"] / "renamed.txt"
    real.write_bytes(make_jpeg())
    v = ImageInputValidator(allowed_roots=(str(media_tree["root"]),))
    with pytest.raises(MediaInputError, match="unrecognized"):
        v.from_local_path(str(fake))
    img = v.from_local_path(str(real))
    assert img.mime == "image/jpeg"  # 按内容判定，不按扩展名


def test_pixel_bomb_rejected(media_tree):
    """声明 30000x30000 的 PNG 头（文件极小）→ 尺寸/像素防线拦截。"""
    bomb = media_tree["root"] / "bomb.png"
    bomb.write_bytes(make_png(30000, 30000))
    v = ImageInputValidator(allowed_roots=(str(media_tree["root"]),))
    with pytest.raises(MediaInputError, match="dimension"):
        v.from_local_path(str(bomb))


# ---------- URL 下载（http_guard 门控） ----------


class FakeResponse:
    def __init__(self, body: bytes, content_type: str = "image/png"):
        self._buf = io.BytesIO(body)
        self.headers = {"Content-Type": content_type}

    def read(self, n=-1):
        return self._buf.read(n)

    def close(self):
        pass


class FakeOpener:
    """脚本化 opener：{url: FakeResponse | (status, location)}，记录请求。"""

    def __init__(self, routes):
        self.routes = routes
        self.requested: list[str] = []

    def open(self, req, timeout=0):
        url = req.full_url
        self.requested.append(url)
        route = self.routes[url]
        if isinstance(route, tuple):
            status, location = route
            err = urllib.error.HTTPError(url, status, "redirect", {}, None)
            err.headers = {"Location": location}
            raise err
        return route


def _validator(opener, **kw):
    return ImageInputValidator(opener=opener, resolver=public_resolver, **kw)


def test_url_https_only():
    v = _validator(FakeOpener({}))
    with pytest.raises(MediaInputError, match="https"):
        v.from_url("http://example.com/x.png")
    with pytest.raises(MediaInputError, match="https"):
        v.from_url("file:///etc/passwd")


def test_url_valid_image_ok():
    url = "https://cdn.example.com/pic.jpg"
    # Content-Type 声称 png，内容是真 JPEG → 以内容为准
    opener = FakeOpener({url: FakeResponse(make_jpeg(), "image/png")})
    img = _validator(opener).from_url(url)
    assert img.mime == "image/jpeg" and img.origin == "url"


def test_url_ssrf_private_address_blocked():
    def private_resolver(host, port):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", port))]

    v = ImageInputValidator(
        opener=FakeOpener({}), resolver=private_resolver
    )
    with pytest.raises(MediaInputError, match="SSRF"):
        v.from_url("https://metadata.example.com/latest")


def test_url_redirect_each_hop_recchecked():
    """302 绕回内网：跳转目标必须重新过 SSRF 门控。"""
    start = "https://cdn.example.com/a.png"

    def resolver(host, port):
        if "internal" in host:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.8", port))]
        return public_resolver(host, port)

    opener = FakeOpener({start: (302, "https://internal.example.com/loot")})
    v = ImageInputValidator(opener=opener, resolver=resolver)
    with pytest.raises(MediaInputError, match="SSRF"):
        v.from_url(start)
    assert opener.requested == [start]  # 第二跳在请求前就被拦下


def test_url_redirect_chain_to_valid_image():
    start = "https://cdn.example.com/a"
    final = "https://cdn.example.com/b.png"
    opener = FakeOpener({start: (302, final), final: FakeResponse(make_png())})
    img = _validator(opener).from_url(start)
    assert img.mime == "image/png"
    assert opener.requested == [start, final]


def test_url_too_many_redirects():
    url = "https://cdn.example.com/loop"
    opener = FakeOpener({url: (302, url)})
    with pytest.raises(MediaInputError, match="redirect"):
        _validator(opener).from_url(url)


def test_url_body_size_limit():
    url = "https://cdn.example.com/big.png"
    opener = FakeOpener({url: FakeResponse(make_png() + b"\x00" * 4096)})
    with pytest.raises(MediaInputError, match="size limit"):
        _validator(opener, max_bytes=64).from_url(url)


def test_url_non_image_body_rejected():
    url = "https://cdn.example.com/x.png"
    opener = FakeOpener({url: FakeResponse(b"<html>attack</html>")})
    with pytest.raises(MediaInputError, match="unrecognized"):
        _validator(opener).from_url(url)


# ---------- QQ 工作流接线（附件 → 验证器） ----------


def test_qq_workflow_attachment_boundary(media_tree):
    """合法附件通过；白名单外/逃逸/恶意附件被跳过且不阻断整轮。"""
    from whale_core.life.channels import OwnerEvent
    from whale_core.life.qq_workflow import QqInboundWorkflow
    from whale_core.memory.store import InMemoryStore

    class _StubBrain:
        def reply(self, text, *, persona="", memories=(), history=(), **kw):
            return "ok"

    workflow = QqInboundWorkflow(
        InMemoryStore(),
        _StubBrain(),
        media_validator=ImageInputValidator(
            allowed_roots=(str(media_tree["root"]),)
        ),
    )
    event = OwnerEvent(
        owner_id="owner",
        channel="qq",
        text="看图",
        conversation="c1",
        message_id="m1",
        attachments=(
            {"kind": "pic", "file_path": str(media_tree["root"] / "pic.png")},
            {"kind": "pic", "file_path": str(media_tree["outside"])},      # 白名单外
            {"kind": "pic", "file_path": str(media_tree["root"] / "escape.png")},  # 逃逸
            {"kind": "pic", "file_path": "/etc/hostname"},                  # 非图片
            {"kind": "file", "file_path": str(media_tree["root"] / "pic.png")},  # 非 pic 类型
        ),
    )
    images = workflow._attachment_images(event)
    assert len(images) == 1
    data, mime = images[0]
    assert mime == "image/png" and data.startswith(b"\x89PNG")
