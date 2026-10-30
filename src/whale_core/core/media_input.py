"""媒体输入可信边界——图片附件的统一验证器（G0-B）。

所有进入模型的图片（QQ 附件、后续 Web 上传、渠道适配器）必须经过本模块，
不信任来源方声称的 file_path / MIME / 扩展名 / URL：

- **本地路径**：``allowed_roots`` 白名单目录（fail-closed：未配置即拒绝
  一切本地读取）；``realpath`` 解析后必须在白名单内（符号链接逃逸、
  ``..`` 穿越一律拒绝）；普通文件、体积上限；
- **外部 URL**：仅 https，走核心 ``http_guard`` 的 SSRF 肯定式门控 +
  连接层 peer 复核 + 手动重定向逐跳校验（≤3 跳）+ 流式限量读取；
- **内容**：按 magic bytes 识别 PNG/JPEG/GIF/WebP/BMP（扩展名与
  Content-Type 均不可信），解析真实像素尺寸，拒绝超限尺寸/像素总量
  （解压炸弹防线）。

验证失败一律 ``MediaInputError``；调用方逐附件容错（跳过该附件并向用户
诚实说明），不让单张恶意附件打断整轮对话。
"""
from __future__ import annotations

import os
import struct
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from whale_core.core.http_guard import (
    HttpGuardError,
    UrlPolicy,
    build_guarded_opener,
    read_limited,
)

DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_MAX_DIMENSION = 20000
DEFAULT_MAX_PIXELS = 100_000_000  # 解压炸弹防线（约 100MP）
DEFAULT_MAX_REDIRECTS = 3
USER_AGENT = "whale-core/0.1 (+https://github.com/amwangfan/whale-core)"
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})

_FORMAT_MIME = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
}


class MediaInputError(ValueError):
    """媒体输入未通过可信边界校验。"""


@dataclass(frozen=True)
class ImageInput:
    """一张通过验证的图片：真实 MIME 与像素尺寸以内容嗅探为准。"""

    data: bytes
    mime: str
    width: int
    height: int
    origin: str  # "local" | "url"


# ---------- 内容嗅探（magic bytes + 尺寸解析） ----------


def _sniff_png(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _sniff_gif(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10 or data[:6] not in (b"GIF87a", b"GIF89a"):
        return None
    width, height = struct.unpack("<HH", data[6:10])
    return width, height


def _sniff_bmp(data: bytes) -> tuple[int, int] | None:
    if len(data) < 26 or data[:2] != b"BM":
        return None
    width, height = struct.unpack("<ii", data[18:26])
    return abs(width), abs(height)


def _sniff_jpeg(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    # 逐段扫描到 SOF（C0-C3/C5-C7/C9-CB/CD-CF），拒绝畸形长度
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if i + 4 > len(data):
            return None
        seg_len = struct.unpack(">H", data[i + 2 : i + 4])[0]
        if seg_len < 2 or i + 2 + seg_len > len(data):
            return None
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if seg_len < 7:
                return None
            height, width = struct.unpack(">HH", data[i + 5 : i + 9])
            return width, height
        i += 2 + seg_len
    return None


def _sniff_webp(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
        width = struct.unpack("<H", data[26:28])[0] & 0x3FFF
        height = struct.unpack("<H", data[28:30])[0] & 0x3FFF
        return width, height
    if chunk == b"VP8L" and data[20] == 0x2F:
        b = data[21:25]
        width = 1 + (((b[1] & 0x3F) << 8) | b[0])
        height = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
        return width, height
    return None


_SNIFFERS = (
    ("png", _sniff_png),
    ("jpeg", _sniff_jpeg),
    ("gif", _sniff_gif),
    ("webp", _sniff_webp),
    ("bmp", _sniff_bmp),
)


def sniff_image(data: bytes) -> tuple[str, int, int]:
    """按内容识别图片格式与尺寸；无法识别抛 MediaInputError。"""
    for fmt, sniffer in _SNIFFERS:
        dims = sniffer(data)
        if dims is not None:
            width, height = dims
            if width > 0 and height > 0:
                return fmt, width, height
            raise MediaInputError(f"corrupt {fmt} header")
    raise MediaInputError("unrecognized image format (not PNG/JPEG/GIF/WebP/BMP)")


class ImageInputValidator:
    """图片输入统一验证器（本地白名单路径 + 受门控的 https 下载）。

    网络层可注入（``opener``/``resolver``），测试不触网。
    """

    def __init__(
        self,
        *,
        allowed_roots: tuple[str, ...] = (),
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_dimension: int = DEFAULT_MAX_DIMENSION,
        max_pixels: int = DEFAULT_MAX_PIXELS,
        max_redirects: int = DEFAULT_MAX_REDIRECTS,
        timeout: float = 20.0,
        allow_private: bool = False,
        opener: Any = None,
        resolver: Callable[[str, int], Any] | None = None,
    ) -> None:
        if max_bytes <= 0 or max_dimension <= 0 or max_pixels <= 0:
            raise ValueError("invalid media limits")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._roots = tuple(
            os.path.realpath(root) for root in allowed_roots if str(root).strip()
        )
        self._max_bytes = int(max_bytes)
        self._max_dimension = int(max_dimension)
        self._max_pixels = int(max_pixels)
        self._max_redirects = int(max_redirects)
        self._timeout = float(timeout)
        self._policy = UrlPolicy(
            allow_private=allow_private, schemes=("https",), resolver=resolver
        )
        self._opener = (
            opener if opener is not None else build_guarded_opener(allow_private)
        )

    # ---- 内容校验 ----

    def validate_bytes(self, data: bytes, *, origin: str) -> ImageInput:
        if not data:
            raise MediaInputError("empty image data")
        if len(data) > self._max_bytes:
            raise MediaInputError("image exceeds size limit")
        fmt, width, height = sniff_image(data)
        if width > self._max_dimension or height > self._max_dimension:
            raise MediaInputError("image dimensions exceed limit")
        if width * height > self._max_pixels:
            raise MediaInputError("image pixel count exceeds limit (bomb guard)")
        return ImageInput(
            data=data, mime=_FORMAT_MIME[fmt], width=width, height=height,
            origin=origin,
        )

    # ---- 本地路径（白名单 fail-closed） ----

    def from_local_path(self, path: str) -> ImageInput:
        raw = (path or "").strip()
        if not raw:
            raise MediaInputError("empty local path")
        if not self._roots:
            raise MediaInputError("no allowed media roots configured")
        real = os.path.realpath(raw)  # 解析符号链接与 ..
        if not self._within_roots(real):
            raise MediaInputError("local path outside allowed roots")
        try:
            if not os.path.isfile(real):
                raise MediaInputError("not a regular file")
            size = os.path.getsize(real)
            if size > self._max_bytes:
                raise MediaInputError("image exceeds size limit")
            with open(real, "rb") as fh:
                data = fh.read(self._max_bytes + 1)
        except MediaInputError:
            raise
        except OSError as exc:
            # 只读宿主/未授权路径等：不泄露 errno 细节之外的系统信息
            raise MediaInputError(
                f"local media unreadable: {type(exc).__name__}"
            ) from None
        return self.validate_bytes(data, origin="local")

    def _within_roots(self, real: str) -> bool:
        for root in self._roots:
            try:
                if os.path.commonpath((real, root)) == root:
                    return True
            except ValueError:
                continue  # 不同盘符/混合相对路径
        return False

    # ---- 外部 URL（http_guard 门控） ----

    def from_url(self, url: str) -> ImageInput:
        import time as _time

        current = (url or "").strip()
        redirects = 0
        deadline = _time.monotonic() + self._timeout * (self._max_redirects + 1) + 5.0
        while True:
            if _time.monotonic() > deadline:
                raise MediaInputError("total deadline exceeded")
            try:
                self._policy.check(current)  # 每一跳都重新校验
            except HttpGuardError as exc:
                raise MediaInputError(str(exc)) from None
            request = urllib.request.Request(
                current,
                headers={
                    "User-Agent": USER_AGENT,
                    "Accept": "image/*",
                    "Accept-Encoding": "identity",
                },
            )
            try:
                response = self._opener.open(request, timeout=self._timeout)
            except urllib.error.HTTPError as exc:
                if exc.code in _REDIRECT_CODES:
                    if redirects >= self._max_redirects:
                        raise MediaInputError(
                            f"too many redirects (>{self._max_redirects})"
                        ) from None
                    location = exc.headers.get("Location") if exc.headers else None
                    if not location:
                        raise MediaInputError(
                            f"HTTP {exc.code}: missing Location"
                        ) from None
                    current = urllib.parse.urljoin(current, str(location))
                    redirects += 1
                    continue
                raise MediaInputError(f"HTTP error {exc.code}") from None
            except HttpGuardError as exc:
                raise MediaInputError(str(exc)) from None
            except Exception as exc:
                raise MediaInputError(
                    f"request failed: {type(exc).__name__}"
                ) from None
            try:
                body = read_limited(response, self._max_bytes + 1, deadline)
            except HttpGuardError as exc:
                raise MediaInputError(str(exc)) from None
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
            return self.validate_bytes(body, origin="url")
