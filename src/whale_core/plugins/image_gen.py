"""生图网关：提示词 + 参考图（可选、多张）+ 模型选择的单文件收口。

分层：内置 provider 编排（宿主装配点），非插件框架契约（builtin-provider，issue #31）。

2026-11-05 owner 定版的生图流程（群消息 → 出图）：

1. 群里有消息，可能没明说作图但有这个含义 → 意图模型（kimi-k3）分析出来；
2. kimi 生成作图提示词，并判定主体是不是已知角色/人物/IP 形象——
   是 → 填 ``reference_query``（参考图搜索词），或者自己经 web 搜索
   拿到图片链接后直接给 ``reference_urls``；否 → 都不填；
3. 进入本模块 ``ImageGenGateway.generate``：
   - 有参考图（链接逐张下载校验 + 搜索词搜图补齐，合计最多 3 张）→ 带图生成；
   - 一张都没有 → 纯文生图；
   - 模型：默认 qwen-image-3.0（带参考图还原度实测明显更好），
     可选 minimax-image-01（subject_reference 只认一张 → 取首张）。

Provider 适配：声明 ``supports_references = True`` 的 provider 收
``references=[ReferenceImage, ...]``；没声明的（如 OpenAI Images）参考图被丢弃，
结果 ``note`` 如实标注降级。
"""
from __future__ import annotations

import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from whale_core.plugins.media_image import ImageTaskResult, ReferenceImage

#: 单次生成最多携带的参考图张数（qwen-image-3.0 多图输入实测 3 张内稳定；
#: MiniMax 只取首张——见 minimax_media 的 subject_reference 契约）。
MAX_REFERENCES = 3
#: 搜索候选拉取数（逐个下载校验，凑够 MAX_REFERENCES 张可用即停）。
_SEARCH_CANDIDATES = 8
_MAX_REF_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class ImageGenResult:
    """网关层结果：provider 结果 + 参考图使用的如实标注。"""

    task_id: str
    status: str
    images: tuple[str, ...]
    model: str
    reference_used: bool
    reference_count: int = 0
    note: str = ""


def sniff_image_mime(data: bytes) -> str:
    """光栅图魔数嗅探：png/jpeg/gif/webp → mime；其它 → 空串。"""
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def fetch_reference_image(url: str, *, max_bytes: int = _MAX_REF_BYTES,
                          timeout: float = 20.0) -> ReferenceImage | None:
    """下载一张参考图：仅 https、大小封顶、魔数必须是光栅图；失败 → None。"""
    if not str(url or "").startswith("https://"):
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "whale-core/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read(max_bytes + 1)
        if not data or len(data) > max_bytes:
            return None
        mime = sniff_image_mime(data)
        return ReferenceImage(data=data, mime=mime) if mime else None
    except Exception:
        return None


def find_reference_images(query: str, *,
                          search_images: Callable[..., Sequence[str]],
                          fetcher: Callable[..., ReferenceImage | None] = fetch_reference_image,
                          limit: int = MAX_REFERENCES,
                          candidates: int = _SEARCH_CANDIDATES) -> list[ReferenceImage]:
    """按检索词搜参考图（图片搜索通道），凑够 limit 张可用即停；找不到 → []。"""
    if not callable(search_images):
        return []
    try:
        urls = search_images(query, limit=candidates) or []
    except Exception:
        return []
    refs: list[ReferenceImage] = []
    for url in list(urls)[:candidates]:
        got = fetcher(url)
        if got is not None:
            refs.append(got)
            if len(refs) >= limit:
                break
    return refs


class ImageGenGateway:
    """生图统一入口：模型分派 + 参考图采集 + 降级标注。

    入参语义（对应工具参数）：
    - ``prompt``：作图提示词（必填）；
    - ``model``：生图模型名，空 = 默认模型（未知模型 ValueError 并列出可用）；
    - ``reference_query``：参考图搜索词（主体是已知角色/IP 时由意图模型给）；
    - ``reference_urls``：参考图链接列表（意图模型自己搜到图时直接给）；
    - ``size`` / ``n``：尺寸与张数（默认随模型供应商）。
    """

    def __init__(self, providers: Mapping[str, object], *, default_model: str,
                 default_size: str = "1328*1328",
                 image_search: Callable[..., Sequence[str]] | None = None,
                 fetcher: Callable[..., ReferenceImage | None] = fetch_reference_image,
                 max_references: int = MAX_REFERENCES) -> None:
        if default_model and default_model not in providers:
            raise ValueError(f"default model {default_model!r} not in providers")
        self.providers = dict(providers)
        self.default_model = default_model
        self.default_size = default_size
        self.image_search = image_search
        self.fetcher = fetcher
        self.max_references = max(1, int(max_references))

    @property
    def available_models(self) -> list[str]:
        return sorted(self.providers)

    def collect_references(self, *, reference_query: str = "",
                           reference_urls: Sequence[str] = ()) -> list[ReferenceImage]:
        """参考图采集：先直接链接，再搜索词补齐；合计封顶 max_references。"""
        refs: list[ReferenceImage] = []
        for url in list(reference_urls or ())[: self.max_references]:
            got = self.fetcher(str(url))
            if got is not None:
                refs.append(got)
        if reference_query and len(refs) < self.max_references and self.image_search:
            refs.extend(find_reference_images(
                reference_query[:200], search_images=self.image_search,
                fetcher=self.fetcher,
                limit=self.max_references - len(refs)))
        return refs[: self.max_references]

    def generate(self, prompt: str, *, model: str = "",
                 reference_query: str = "",
                 reference_urls: Sequence[str] = (),
                 size: str = "", n: int = 1) -> ImageGenResult:
        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("prompt must be non-empty")
        model = (model or "").strip() or self.default_model
        provider = self.providers.get(model)
        if provider is None:
            raise ValueError(
                f"未知生图模型 {model!r}（可用：{', '.join(self.available_models)}）")

        refs: list[ReferenceImage] = []
        if reference_query or reference_urls:
            refs = self.collect_references(
                reference_query=reference_query, reference_urls=reference_urls)

        note = ""
        gen_kw = dict(size=size or self.default_size, n=max(1, min(int(n or 1), 4)))
        if refs and getattr(provider, "supports_references", False):
            result: ImageTaskResult = provider.generate(prompt, references=refs, **gen_kw)
        else:
            if refs:
                note = "该模型不支持参考图，已按纯描述生成。"
            elif reference_query or reference_urls:
                note = "没搜到可用参考图，按纯描述生成。"
            result = provider.generate(prompt, **gen_kw)
        return ImageGenResult(
            task_id=result.task_id, status=result.status, images=result.images,
            model=model, reference_used=bool(refs) and not note,
            reference_count=len(refs) if not note else 0, note=note)
