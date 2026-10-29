"""media.image.generate 能力契约 + 官方图像生成插件（PR-12，roadmap §8 P1-5）。

分层约定（与仓内既有约定一致：护栏在核心，落盘在宿主）：

- 本层只返回产物**引用**（URL 或 ``data:`` base64 引用字符串）；**不落盘**——
  产物的下载、存储与过期清理由宿主负责；
- base_url 策略与 ``core/deepseek.py`` 一致：公网必须 https，回环/私网 http
  放行（本地/自建兼容端点）；
- 错误一律 ``ImageError``（kind 分类：config/auth/quota/server/network/
  bad_response），消息含状态码 / 错误类型，**绝不含 api_key**；
- 网络层走 ``urllib.request.urlopen``，测试用 monkeypatch 替身、不触网。
"""

from __future__ import annotations

import html
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Protocol

from whale_core.plugins.manifest import PluginManifest
from whale_core.plugins.registry import PluginRegistry

PLUGIN_ID = "media_image"
IMAGE_GENERATE = "media.image.generate"

_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_MAX_PROMPT_CHARS = 4000
_MIN_N = 1
_MAX_N = 4
ALLOWED_SIZES: tuple[str, ...] = ("1024x1024", "1792x1024", "1024x1792")
_ALLOWED_SIZE_SET = frozenset(ALLOWED_SIZES)


@dataclass(frozen=True)
class ImageTaskResult:
    """一次图像生成任务的结果。

    ``images`` 为产物**引用**（远端 URL 或 ``data:`` base64 引用字符串），
    本层不落盘——产物下载、存储与过期清理是宿主职责。
    本模块实现失败时抛 ``ImageError`` 而非返回 ``status="failed"``；
    "failed" 保留给宿主聚合层（部分成功 / 异步任务轮询）使用。
    """

    task_id: str
    status: str  # "succeeded" | "failed"
    images: tuple[str, ...]
    error: str = ""


class ImageError(RuntimeError):
    """图像生成调用失败（分类化）。

    kind: config（未配置凭据）/ auth（401/403）/ quota（429 配额或限流）/
    server（5xx）/ network（连接/超时）/ bad_response（响应形状或内容不符）。
    消息只含状态码与错误类型名，绝不含 api_key。
    """

    def __init__(self, kind: str, message: str, *, status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


class ImageProvider(Protocol):
    """media.image.generate 能力提供者契约。

    行为约定（所有实现必须遵守）：prompt 必填非空并截断 4000 字符；
    size 白名单校验（未知值 ValueError）；n 钳制到 [1, 4]。
    """

    def generate(
        self, prompt: str, *, size: str = "1024x1024", n: int = 1
    ) -> ImageTaskResult: ...


def _is_local_or_private_http(url: str) -> bool:
    """http:// 仅放行回环与私网地址（local 配置档；公网 http 仍拒绝）。

    本函数镜像 ``whale_core.core.deepseek._is_local_or_private_http``
    （该助手为模块私有，故在此保留一份拷贝，修改时保持两份一致）。
    """
    try:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme.lower() != "http":
            return False
        host = (parts.hostname or "").strip().lower()
    except ValueError:
        return False
    if host == "localhost":
        return True
    import ipaddress
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False  # 主机名一律不算私网：先解析成字面 IP 才谈归属（安全审查修正）
    return ip.is_loopback or ip.is_private


def _validate_request(prompt: str, size: str, n: int) -> tuple[str, str, int]:
    """契约护栏：prompt 必填非空 + 截断 4000；size 白名单；n 钳制 [1,4]。"""
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("prompt must be non-empty")
    prompt = prompt[:_MAX_PROMPT_CHARS]
    if size not in _ALLOWED_SIZE_SET:
        raise ValueError(
            f"unsupported size {size!r}: allowed sizes are {sorted(_ALLOWED_SIZE_SET)}"
        )
    n = max(_MIN_N, min(_MAX_N, int(n)))
    return prompt, size, n


def _parse_images(data: object) -> tuple[str, ...]:
    """解析 OpenAI Images 形状 ``{"data": [{"url"|"b64_json"}, ...]}``。

    ``b64_json`` 转成 ``data:image/png;base64,...`` 引用字符串；
    全部条目两者都缺（或形状不符）→ ImageError(bad_response)。
    """
    items = data.get("data") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ImageError("bad_response", "image response shape unexpected")
    images: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = item.get("url")
        if isinstance(url, str) and url:
            images.append(url)
            continue
        b64 = item.get("b64_json")
        if isinstance(b64, str) and b64:
            images.append("data:image/png;base64," + b64)
    if not images:
        raise ImageError("bad_response", "image response contains no url or b64_json")
    return tuple(images)


class OpenAIImagesProvider:
    """OpenAI Images 兼容端点：POST {base}/images/generations，Bearer key 认证。

    base_url 策略：公网必须 https；回环/私网 http 放行（本地/自建兼容端点，
    且此类端点允许空 api_key，与 core/deepseek.py 同款约定）。
    """

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = _DEFAULT_BASE_URL,
        model: str = "dall-e-3",
        timeout: float = 60.0,
    ):
        self.api_key = api_key or ""
        self.base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        if not (
            self.base_url.startswith("https://")
            or _is_local_or_private_http(self.base_url)
        ):
            raise ValueError("image provider requires HTTPS (or loopback/private HTTP)")
        self.model = model
        self.timeout = timeout

    def generate(
        self, prompt: str, *, size: str = "1024x1024", n: int = 1
    ) -> ImageTaskResult:
        prompt, size, n = _validate_request(prompt, size, n)
        if not self.api_key and not _is_local_or_private_http(self.base_url):
            raise ImageError("config", "image api key not configured")
        body = json.dumps(
            {"model": self.model, "prompt": prompt, "size": size, "n": n},
            ensure_ascii=False,
        ).encode()
        data = self._post(body)
        return ImageTaskResult(
            task_id="img-" + uuid.uuid4().hex[:12],
            status="succeeded",
            images=_parse_images(data),
        )

    def _post(self, body: bytes) -> object:
        req = urllib.request.Request(
            self.base_url + "/images/generations",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return json.loads(response.read(8 * 1024 * 1024).decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            status = exc.code
            if status in (401, 403):
                raise ImageError("auth", f"image auth failed ({status})", status=status) from exc
            if status == 429:
                raise ImageError(
                    "quota", "image quota exhausted or rate limited (429)", status=429
                ) from exc
            if 500 <= status < 600:
                raise ImageError("server", f"image server error ({status})", status=status) from exc
            raise ImageError("bad_response", f"image HTTP error ({status})", status=status) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            # 消息只含异常类型名，不含 URL/凭据
            raise ImageError("network", f"image network error: {type(exc).__name__}") from exc
        except json.JSONDecodeError as exc:
            raise ImageError("bad_response", "image endpoint returned invalid JSON") from exc


def _mock_svg_data_uri(prompt: str, size: str, index: int) -> str:
    """确定性罐装产物：内嵌 prompt 摘要的 SVG data URI（mock 专用）。"""
    width, _, height = size.partition("x")
    label = html.escape(f"mock #{index + 1}: {prompt[:60]}", quote=False)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}">'
        f'<rect width="100%" height="100%" fill="#dfe9f3"/>'
        f'<text x="16" y="32" font-size="20" fill="#334455">{label}</text>'
        f"</svg>"
    )
    return "data:image/svg+xml," + urllib.parse.quote(svg, safe="")


class MockImageProvider:
    """**Mock**（罐装产物，测试/离线开发专用）：不发起任何网络请求。

    返回确定性 SVG ``data:`` URI 产物引用——同参数多次调用结果完全相等。
    产物不落盘；存储与过期清理由宿主负责。真实端点请用 OpenAIImagesProvider。
    """

    def generate(
        self, prompt: str, *, size: str = "1024x1024", n: int = 1
    ) -> ImageTaskResult:
        prompt, size, n = _validate_request(prompt, size, n)
        images = tuple(_mock_svg_data_uri(prompt, size, i) for i in range(n))
        return ImageTaskResult(task_id="mock-image-task", status="succeeded", images=images)


def register_image_gen(
    registry: PluginRegistry, *, provider: object = None
) -> object:
    """注册 media.image.generate 能力（model 类插件），返回接线好的 provider。

    ``provider`` 缺省时挂 **MockImageProvider**（罐装 mock 产物，仅供契约先行 /
    离线开发，manifest 描述中明确标注）；真实部署由宿主构造
    ``OpenAIImagesProvider`` 传入（凭据只在宿主 env / 私有配置，manifest 不含）。
    重复注册同 plugin_id 由 PluginRegistry 既有约束抛 ValueError。
    """
    defaulted = provider is None
    if defaulted:
        provider = MockImageProvider()
    manifest = PluginManifest(
        plugin_id=PLUGIN_ID,
        name="图像生成（media.image.generate）",
        version="0.1.0",
        author="whale-core",
        description=(
            "图像生成能力（**mock**：罐装 SVG 产物，仅供离线开发/契约先行）"
            if defaulted
            else "图像生成能力（OpenAI Images 兼容端点，宿主接线 provider）"
        ),
        kind="model",
        provides=(IMAGE_GENERATE,),
    )
    registry.register(manifest, provider=provider)
    return provider
