"""Dynamically resolve optional semantic recall without replacing curation writes."""
from __future__ import annotations
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.hybrid import HybridCurator


class DynamicHybridCurator:
    def __init__(self,store,registry,*,base=None,alpha: float=0.6):
        self.base=base or MemoryCurator(store)
        self.registry=registry
        self.alpha=alpha
        self.store=store

    def __getattr__(self,name):
        # remember(), half_life_days and future curation methods retain their original
        # source/evidence/consent semantics rather than duplicating fact-writing code.
        return getattr(self.base,name)

    def recall(self,query: str,*,limit: int=5,now=None,allowed_spaces=None,include_protected=False):
        if limit <= 0:
            return ()
        providers=self.registry.providers_for('embedding.embed')
        provider=next((item for _,item in providers if item is not None),None)
        # Empty queries are stable-fact lookup; sending them to /embeddings is wasteful
        # and most endpoints reject empty input. Explicit historical time stays lexical.
        # S2：allowed_spaces 原样透传——回退只在**已授权空间集合内**发生，
        # 能力降级（向量→词项）绝不扩大空间范围（TH-06）。
        if provider is None or not query.strip() or now is not None:
            return self.base.recall(query,limit=limit,now=now,allowed_spaces=allowed_spaces,include_protected=include_protected)
        try:
            hybrid=HybridCurator(self.store,embedder=provider,alpha=self.alpha,
                                 half_life_days=self.base.half_life_days)
            return hybrid.recall(query,limit=limit,allowed_spaces=allowed_spaces,include_protected=include_protected)
        except Exception:
            # External optional retrieval never takes down the local memory path.
            return self.base.recall(query,limit=limit,now=now,allowed_spaces=allowed_spaces,include_protected=include_protected)

    def forget(self,key: str) -> bool:
        record=self.store.get_record(key)
        forgotten=self.base.forget(key)
        if forgotten and record is not None:
            summary=str(record.content.get('summary',''))
            for _,provider in self.registry.providers_for('embedding.embed'):
                purge=getattr(provider,'forget_text',None)
                if callable(purge):
                    purge(summary)
        return forgotten
