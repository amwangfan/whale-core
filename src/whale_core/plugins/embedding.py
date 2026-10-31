"""OpenAI-compatible embedding provider with bounded requests and hash-only LRU.

The cache never stores source text. It is process-local, bounded and clearable;
provider registration is explicit and does not enable external calls by default.
"""
from __future__ import annotations
from collections import OrderedDict
import hashlib
import json
import math
import threading
import time
import urllib.error
import urllib.request

from whale_core.plugins import provider_http
from whale_core.plugins.manifest import PluginManifest


class EmbeddingError(RuntimeError):
    def __init__(self,kind: str,message: str):
        super().__init__(message)
        self.kind=kind


class OpenAICompatibleEmbedding:
    def __init__(self, *, base_url: str, api_key: str = "", model: str = "text-embedding-3-small",
                 timeout: float = 8, batch_size: int = 32, cache_size: int = 512,
                 max_inputs: int = 512):
        self.base_url=provider_http.validate_base_url(base_url)
        if not model.strip() or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError("embedding model and finite positive timeout required")
        if not 1 <= batch_size <= 128 or not 0 <= cache_size <= 4096 or not 1 <= max_inputs <= 4096:
            raise ValueError("invalid embedding limits")
        self.api_key,self.model,self.timeout=api_key,model,timeout
        self.batch_size,self.cache_size,self.max_inputs=batch_size,cache_size,max_inputs
        self._cache: OrderedDict[str,tuple[float,...]]=OrderedDict()
        self._dimension: int | None=None
        self._lock=threading.RLock()
        # S2：text_hash → {cache_key,...} 反向索引，支撑 forget_text 跨命名空间清除。
        self._text_keys: dict[str,set[str]]={}

    @staticmethod
    def _key(text: str, namespace: str = "") -> str:
        # S2：缓存分区键 = sha256(namespace \0 text)。同一文本在不同记忆
        # 空间的缓存项互不复用（INV-08）。
        return hashlib.sha256(namespace.encode('utf-8')+b'\x00'+text.encode('utf-8')).hexdigest()

    @staticmethod
    def _text_hash(text: str) -> str:
        return hashlib.sha256(text.encode('utf-8')).hexdigest()

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
            self._text_keys.clear()

    def forget_text(self,text: str) -> None:
        """清除某段文本在**所有命名空间**的缓存向量（治理删除传播）。"""
        with self._lock:
            for key in self._text_keys.pop(self._text_hash(text),()):
                self._cache.pop(key,None)

    def _evict(self,key: str) -> None:
        self._cache.pop(key,None)

    def _track(self,key: str,text: str) -> None:
        self._text_keys.setdefault(self._text_hash(text),set()).add(key)

    def _request(self,texts: list[str],timeout: float) -> list[list[float]]:
        from whale_core.plugins.speech import _is_local_or_private_http
        if not self.api_key and not _is_local_or_private_http(self.base_url):
            raise EmbeddingError('config','embedding API key not configured')
        req=urllib.request.Request(self.base_url+'/embeddings',method='POST',
            data=json.dumps({'model':self.model,'input':texts,'encoding_format':'float'},ensure_ascii=False).encode(),
            headers={'Content-Type':'application/json'})
        if self.api_key:
            req.add_unredirected_header('Authorization','Bearer '+self.api_key)
        try:
            with provider_http.open_request(req,timeout=timeout) as response:
                raw=provider_http.read_response(response,16*1024*1024)
            data=json.loads(raw)
        except urllib.error.HTTPError as exc:
            kind='auth' if exc.code in (401,403) else 'quota' if exc.code==429 else 'server' if exc.code>=500 else 'bad_response'
            raise EmbeddingError(kind,f'embedding HTTP {exc.code}') from None
        except (urllib.error.URLError,OSError) as exc:
            raise EmbeddingError('network','embedding network error:'+type(exc).__name__) from None
        except (ValueError,UnicodeError):
            raise EmbeddingError('bad_response','invalid or oversized embedding response') from None
        items=data.get('data') if isinstance(data,dict) else None
        if not isinstance(items,list) or len(items)!=len(texts):
            raise EmbeddingError('bad_response','embedding response quantity mismatch')
        result: list[list[float] | None]=[None]*len(texts)
        dimension=None
        for item in items:
            if not isinstance(item,dict):
                raise EmbeddingError('bad_response','invalid embedding item')
            index,vector=item.get('index'),item.get('embedding')
            if type(index) is not int or not 0 <= index < len(texts) or result[index] is not None:
                raise EmbeddingError('bad_response','invalid or duplicate embedding index')
            if not isinstance(vector,list) or not 1 <= len(vector) <= 4096:
                raise EmbeddingError('bad_response','invalid embedding dimension')
            if any(type(value) not in (int,float) or abs(value)>1e100 or not math.isfinite(value) for value in vector):
                raise EmbeddingError('bad_response','embedding contains non-finite values')
            if dimension is not None and len(vector)!=dimension:
                raise EmbeddingError('bad_response','embedding dimensions differ')
            dimension=len(vector)
            result[index]=[float(value) for value in vector]
        return result

    def embed(self,texts: list[str],*,namespace: str="") -> list[list[float]]:
        if not isinstance(texts,list) or len(texts)>self.max_inputs:
            raise ValueError('embedding inputs exceed item limit')
        if not texts:
            return []
        if any(not isinstance(text,str) or not text.strip() or len(text)>8000 for text in texts):
            raise ValueError('embedding texts must contain 1..8000 characters')
        if sum(len(text) for text in texts)>512*1024:
            raise ValueError('embedding request exceeds text budget')
        keys=[self._key(text,namespace) for text in texts]
        # One provider call at a time bounds concurrent network work and cache races.
        with self._lock:
            deadline=time.monotonic()+self.timeout
            found={key:self._cache[key] for key in keys if key in self._cache}
            missing: dict[str,str]={}
            for key,text in zip(keys,texts):
                if key not in found:
                    missing[key]=text
            pending=list(missing.items())
            learned={}
            dimension=self._dimension
            for start in range(0,len(pending),self.batch_size):
                remaining=deadline-time.monotonic()
                if remaining <= 0:
                    raise EmbeddingError('network','embedding total deadline exceeded')
                batch=pending[start:start+self.batch_size]
                vectors=self._request([text for _,text in batch],remaining)
                for (key,_),vector in zip(batch,vectors):
                    if dimension is not None and len(vector)!=dimension:
                        raise EmbeddingError('bad_response','embedding dimension changed')
                    dimension=len(vector)
                    learned[key]=tuple(vector)
            # Commit only after every batch validated. No partially cached invalid reply.
            self._dimension=dimension
            found.update(learned)
            for key,text in zip(keys,texts):
                if self.cache_size:
                    self._cache[key]=found[key]
                    self._cache.move_to_end(key)
                    self._track(key,text)
            while len(self._cache)>self.cache_size:
                evicted,_=self._cache.popitem(last=False)
                for keys_set in self._text_keys.values():
                    keys_set.discard(evicted)
            return [list(found[key]) for key in keys]


def register_embedding(registry,provider):
    registry.register(PluginManifest(plugin_id='embedding_model',name='语义记忆嵌入',version='1.0.0',
        kind='model',provides=('embedding.embed',),description='OpenAI兼容嵌入；记忆语义召回可选增强'),provider=provider)
    return provider
