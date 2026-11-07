"""移动端聊天附件存储层（contracts/v2 §11 AttachmentRef 的 core 侧落地）。

设计要点：
- ID：``att_`` + 26 字符 ULID（复用 devices/transports/base.py 的 ``new_id``，
  与协议 §3 一致）；**先做严格 pattern 校验再拼路径**，路径穿越无从发生；
- 落盘：``{root}/{attachment_id}.bin``（内容）+ ``{root}/{attachment_id}.json``
  （元数据）。写入走 ``tmp + os.replace`` 原子提交，读不到半成品；
- 删除：blob 立即移除、元数据改写为 tombstone（``deleted_at``），
  GET 据此区分 404（从未存在）/ 410（已删除）；不留孤儿 blob；
- 存储目录可配置：环境变量 ``WHALE_ATTACHMENTS_DIR``，缺省
  ``/opt/whale/state/attachments``（沿用 life/phone_gallery.py 的 state 目录先例）；
- 图片宽高用纯 stdlib 解析 PNG/JPEG/GIF/WebP/BMP 头部（Pillow 是可选 adb
  extra，不作硬依赖）；识别不了的已知图片类型视为损坏，拒绝入库（honest：
  不伪造宽高，schema 里 width/height 本就可选）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import tempfile
from pathlib import Path
from typing import Any

from whale_core.devices.transports.base import new_id, now_ms

# contracts/v2 §3：attachment_id = att_ + ULID（Crockford Base32，不含 I/L/O/U）
ATTACHMENT_ID_RE = re.compile(r"^att_[0-9A-HJKMNP-TV-Z]{26}$")

# §11：单附件 ≤ 50MB（产品约束；schema 上限 512MB 是传输层兜底）
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024

# multipart 除文件本体外允许的开销（边界/头部/字段），Content-Length 预检用
MULTIPART_OVERHEAD_BYTES = 256 * 1024

ATTACHMENT_TYPES = ("image", "video", "audio", "file")

_MIME_RE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")

DEFAULT_DIR = "/opt/whale/state/attachments"


class AttachmentError(Exception):
    """附件存储错误。kind ∈ not_found / gone / bad_request / too_large。"""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def default_root() -> Path:
    """存储根目录：WHALE_ATTACHMENTS_DIR 可配，缺省 state/attachments。"""
    return Path(os.environ.get("WHALE_ATTACHMENTS_DIR") or DEFAULT_DIR)


def classify_mime(mime: str) -> str:
    """按 MIME 主类型归并附件类别（image/video/audio/file）。"""
    major = mime.split("/", 1)[0]
    return major if major in ("image", "video", "audio") else "file"


def sanitize_mime(raw: str) -> str:
    """清洗 multipart 里的 Content-Type：小写、去参数、白名单字符集。"""
    mime = (raw or "").split(";", 1)[0].strip().lower()
    if not _MIME_RE.match(mime) or len(mime) > 128:
        return "application/octet-stream"
    return mime


# ---------------------------------------------------------------------------
# 图片尺寸解析（纯 stdlib，只读头部；失败返回 None，由调用方决定拒绝/放行）
# ---------------------------------------------------------------------------

def _png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return (width, height) if width and height else None
    return None


def _gif_size(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
        width, height = struct.unpack("<HH", data[6:10])
        return (width, height) if width and height else None
    return None


def _bmp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) >= 26 and data[:2] == b"BM":
        width, height = struct.unpack("<ii", data[18:26])
        if width > 0 and height != 0:
            return width, abs(height)
    return None


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    pos = 2
    while pos + 9 < len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        # SOF0-15（排除 DHT/DAC/RST）：段内含精度+高+宽
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if pos + 9 <= len(data):
                height, width = struct.unpack(">HH", data[pos + 5:pos + 9])
                return (width, height) if width and height else None
            return None
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        seg_len = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if seg_len < 2:
            return None
        pos += 2 + seg_len
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    if chunk == b"VP8 " and len(data) >= 30:
        # lossy：帧头 10B 后是 9f 8d 2a 签名与 14bit 宽高
        sig_at = data.find(b"\x9d\x01\x2a", 20, 30)
        if sig_at >= 0 and sig_at + 7 <= len(data):
            width = struct.unpack("<H", data[sig_at + 3:sig_at + 5])[0] & 0x3FFF
            height = struct.unpack("<H", data[sig_at + 5:sig_at + 7])[0] & 0x3FFF
            return (width, height) if width and height else None
        return None
    if chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None


def image_size(data: bytes, mime: str) -> tuple[int, int] | None:
    """按声明的 mime 分派解析；返回 (width, height) 或 None。"""
    parser = {
        "image/png": _png_size,
        "image/jpeg": _jpeg_size,
        "image/gif": _gif_size,
        "image/webp": _webp_size,
        "image/bmp": _bmp_size,
    }.get(mime)
    if parser is None:
        return None
    try:
        return parser(data)
    except (struct.error, IndexError, ValueError):
        return None


# ---------------------------------------------------------------------------
# AttachmentStore
# ---------------------------------------------------------------------------

class AttachmentStore:
    """附件的磁盘存储：blob + 元数据 sidecar，tmp+rename 原子写。"""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # ---- 路径（attachment_id 已过严格 pattern 校验才会走到这里） ----
    def _blob_path(self, attachment_id: str) -> Path:
        return self.root / f"{attachment_id}.bin"

    def _meta_path(self, attachment_id: str) -> Path:
        return self.root / f"{attachment_id}.json"

    @staticmethod
    def _check_id(attachment_id: str) -> str:
        """越界/畸形 id 一律拒绝——这是拼路径前的唯一闸口。"""
        if not isinstance(attachment_id, str) or not ATTACHMENT_ID_RE.match(attachment_id):
            raise AttachmentError("not_found", "no such attachment")
        return attachment_id

    # ---- 写入 ----
    def save(self, data: bytes, mime: str, *, filename: str = "",
             declared_type: str = "") -> dict[str, Any]:
        """校验 → 哈希 → 原子落盘，返回符合 AttachmentRef schema 的 dict。"""
        if not isinstance(data, (bytes, bytearray)):
            raise AttachmentError("bad_request", "attachment body required")
        data = bytes(data)
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise AttachmentError(
                "too_large",
                f"attachment exceeds {MAX_ATTACHMENT_BYTES} bytes")
        mime = sanitize_mime(mime)
        atype = classify_mime(mime)
        if declared_type:
            if declared_type not in ATTACHMENT_TYPES:
                raise AttachmentError("bad_request", "unknown attachment type")
            if declared_type != atype:
                raise AttachmentError(
                    "bad_request",
                    f"declared type {declared_type!r} mismatches mime {mime!r}")
        ref: dict[str, Any] = {
            "attachment_id": new_id("att"),
            "type": atype,
            "mime": mime,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        if atype == "image":
            dims = image_size(data, mime)
            if dims is None and mime in (
                    "image/png", "image/jpeg", "image/gif",
                    "image/webp", "image/bmp"):
                # 声称是常见图片却解不出尺寸 = 内容损坏/伪装，拒绝入库
                raise AttachmentError("bad_request", "invalid image data")
            if dims is not None:
                ref["width"], ref["height"] = dims
        attachment_id = ref["attachment_id"]
        meta = dict(ref)
        meta["filename"] = (filename or "")[:256]
        meta["created_at"] = now_ms()
        self.root.mkdir(parents=True, exist_ok=True)
        # tmp + rename：读路径要么看到完整文件，要么看不到
        blob_tmp = tempfile.NamedTemporaryFile(
            dir=self.root, prefix=".tmp-", delete=False)
        try:
            blob_tmp.write(data)
            blob_tmp.close()
            meta_tmp = tempfile.NamedTemporaryFile(
                dir=self.root, prefix=".tmp-", mode="w",
                encoding="utf-8", delete=False)
            try:
                json.dump(meta, meta_tmp, ensure_ascii=False)
                meta_tmp.close()
                os.replace(blob_tmp.name, self._blob_path(attachment_id))
                os.replace(meta_tmp.name, self._meta_path(attachment_id))
            except BaseException:
                os.unlink(meta_tmp.name)
                raise
        except BaseException:
            if os.path.exists(blob_tmp.name):
                os.unlink(blob_tmp.name)
            raise
        return ref

    # ---- 读取 ----
    def load_meta(self, attachment_id: str) -> dict[str, Any]:
        """读元数据；不存在 → not_found，已删除 → gone。"""
        self._check_id(attachment_id)
        path = self._meta_path(attachment_id)
        if not path.is_file():
            raise AttachmentError("not_found", "no such attachment")
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            raise AttachmentError("not_found", "attachment metadata unreadable")
        if not isinstance(meta, dict):
            raise AttachmentError("not_found", "attachment metadata unreadable")
        if meta.get("deleted_at"):
            raise AttachmentError("gone", "attachment deleted")
        return meta

    def read(self, attachment_id: str) -> tuple[dict[str, Any], bytes]:
        """读元数据 + 内容字节。"""
        meta = self.load_meta(attachment_id)
        blob = self._blob_path(attachment_id)
        if not blob.is_file():
            # 元数据在而 blob 丢了：如实报 gone，不假装存在
            raise AttachmentError("gone", "attachment content missing")
        return meta, blob.read_bytes()

    # ---- 删除 ----
    def delete(self, attachment_id: str) -> dict[str, Any]:
        """删除：blob 移除 + 元数据改写 tombstone（GET 才能区分 404/410）。"""
        meta = self.load_meta(attachment_id)  # 404/410 语义在此统一
        blob = self._blob_path(attachment_id)
        if blob.is_file():
            os.unlink(blob)
        tombstone = dict(meta)
        tombstone["deleted_at"] = now_ms()
        tmp = tempfile.NamedTemporaryFile(
            dir=self.root, prefix=".tmp-", mode="w",
            encoding="utf-8", delete=False)
        try:
            json.dump(tombstone, tmp, ensure_ascii=False)
            tmp.close()
            os.replace(tmp.name, self._meta_path(attachment_id))
        except BaseException:
            os.unlink(tmp.name)
            raise
        return {"attachment_id": attachment_id, "deleted": True}
