import io
import json
import math
import urllib.error
import pytest
from whale_core.memory.curation import MemoryCurator
from whale_core.memory.semantic import DynamicHybridCurator
from whale_core.memory.store import InMemoryStore
from whale_core.plugins import provider_http
from whale_core.plugins.embedding import OpenAICompatibleEmbedding,EmbeddingError,register_embedding
from whale_core.plugins.registry import PluginRegistry
from whale_core.core.runtime import ToolRegistry
from whale_core.plugins.capability_setup import configure_optional_capabilities


def provider(**kwargs):
    return OpenAICompatibleEmbedding(base_url='http://localhost:123/v1',**kwargs)


def transport(monkeypatch,handler):
    calls=[]
    def fake(req,**kw):
        body=json.loads(req.data)
        calls.append(body)
        return io.BytesIO(json.dumps(handler(body),allow_nan=True).encode())
    monkeypatch.setattr(provider_http,'open_request',fake)
    return calls


def test_batches_reordered_results_duplicates_cache_and_bounded_hashes(monkeypatch):
    def handler(body):
        return {'data':[{'index':i,'embedding':[len(text),1]} for i,text in reversed(list(enumerate(body['input'])))]}
    calls=transport(monkeypatch,handler)
    p=provider(batch_size=2,cache_size=2)
    assert p.embed(['a','bb','ccc','a'])==[[1,1],[2,1],[3,1],[1,1]]
    assert [len(c['input']) for c in calls]==[2,1]
    assert len(p._cache)==2 and all(len(k)==64 for k in p._cache)
    assert all(type(k) is str and isinstance(v,tuple) for k,v in p._cache.items())
    before=len(calls)
    assert p.embed(['a'])==[[1,1]] and len(calls)==before
    p.forget_text('a')
    p.embed(['a'])
    assert len(calls)==before+1
    p.clear_cache(); assert not p._cache


@pytest.mark.parametrize('items',[
    [], [{'index':0,'embedding':[]}], [{'index':0,'embedding':[float('nan')]}],
    [{'index':0,'embedding':[float('inf')]}], [{'index':0,'embedding':['1']}],
    [{'index':True,'embedding':[1]}], [{'index':1,'embedding':[1]}],
    [{'index':0,'embedding':[True]}], [{'embedding':[1]}],
])
def test_bad_vector_responses_fail_closed(monkeypatch,items):
    transport(monkeypatch,lambda body:{'data':items})
    p=provider()
    with pytest.raises(EmbeddingError) as exc:p.embed(['x'])
    assert exc.value.kind=='bad_response' and not p._cache


def test_mismatched_dimensions_and_duplicate_indices(monkeypatch):
    for items in ([{'index':0,'embedding':[1]},{'index':1,'embedding':[1,2]}],
                  [{'index':0,'embedding':[1]},{'index':0,'embedding':[2]}]):
        transport(monkeypatch,lambda _,items=items:{'data':items})
        with pytest.raises(EmbeddingError):provider().embed(['a','b'])


def test_dimension_cannot_change_across_batches(monkeypatch):
    calls=transport(monkeypatch,lambda body:{'data':[{'index':0,'embedding':[1]*len(body['input'][0])}]})
    p=provider(batch_size=1)
    with pytest.raises(EmbeddingError):p.embed(['a','bb'])
    assert not p._cache


def test_input_and_response_limits(monkeypatch):
    p=provider()
    for bad in ('str',[''],[' '],['x'*8001],[1],['x']*513):
        with pytest.raises(ValueError):p.embed(bad)
    assert p.embed([])==[]
    monkeypatch.setattr(provider_http,'open_request',lambda *a,**k:io.BytesIO(b'x'*(16*1024*1024+1)))
    with pytest.raises(EmbeddingError):p.embed(['x'])


@pytest.mark.parametrize('code,kind',[(401,'auth'),(403,'auth'),(429,'quota'),(503,'server')])
def test_errors_are_classified_without_key(monkeypatch,code,kind):
    def fail(*a,**kw):raise urllib.error.HTTPError('https://secret',code,'secret-key',{},None)
    monkeypatch.setattr(provider_http,'open_request',fail)
    with pytest.raises(EmbeddingError) as exc:provider(api_key='secret-key').embed(['private'])
    assert exc.value.kind==kind and 'secret-key' not in str(exc.value)


def test_all_batches_share_deadline(monkeypatch):
    clock=[0.0]
    monkeypatch.setattr('whale_core.plugins.embedding.time.monotonic',lambda:clock[0])
    def handler(body):
        clock[0]+=5
        return {'data':[{'index':0,'embedding':[1]}]}
    transport(monkeypatch,handler)
    p=provider(timeout=4,batch_size=1)
    with pytest.raises(EmbeddingError):p.embed(['a','b'])
    assert not p._cache


def test_dynamic_semantic_recall_fact_writes_disable_and_forget(monkeypatch):
    store=InMemoryStore();registry=PluginRegistry();base=MemoryCurator(store)
    curator=DynamicHybridCurator(store,registry,base=base)
    record=curator.remember('I enjoy spicy food',source='user',evidence_id='message1',consent=True,key='fact')
    assert not curator.recall('hot cuisine')
    calls=transport(monkeypatch,lambda body:{'data':[{'index':i,'embedding':[1,0]} for i,_ in enumerate(body['input'])]})
    p=provider();register_embedding(registry,p)
    hits=curator.recall('hot cuisine')
    assert hits and hits[0].record.key=='fact' and hits[0].embedding_score==1
    assert any('嵌入' in reason for reason in hits[0].reasons)
    registry.unregister('embedding_model')
    count=len(calls)
    assert not curator.recall('hot cuisine') and len(calls)==count
    register_embedding(registry,p)
    assert curator.forget('fact')
    assert p._key('I enjoy spicy food') not in p._cache
    assert not curator.recall('hot cuisine')
    with pytest.raises(PermissionError):curator.remember('unconfirmed',source='user',evidence_id='m2')


def test_provider_failure_falls_back_lexical_and_empty_query_no_network(monkeypatch):
    store=InMemoryStore();registry=PluginRegistry();curator=DynamicHybridCurator(store,registry)
    curator.remember('spicy food',source='user',evidence_id='m1',consent=True,stable_fact=True)
    register_embedding(registry,provider())
    calls=[]
    def fail(*a,**kw):calls.append(1);raise OSError('private')
    monkeypatch.setattr(provider_http,'open_request',fail)
    assert curator.recall('spicy')
    assert curator.recall('')
    assert len(calls)==1


def test_embedding_env_gate_registers_provider_without_tools(tmp_path):
    registry=PluginRegistry();tools=ToolRegistry()
    configure_optional_capabilities(registry,tools,InMemoryStore(),env={'WHALE_EMBEDDING_BASE_URL':'http://localhost:8080/v1'},artifact_dir=tmp_path)
    assert registry.providers_for('embedding.embed')[0][0]=='embedding_model'
    assert tools.specs()==[]
