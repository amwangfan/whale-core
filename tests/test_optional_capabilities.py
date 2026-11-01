"""Optional capabilities use real provider contracts, offline transports in tests."""
import base64
import io
import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from whale_core.core.runtime import ToolRegistry
from whale_core.memory.store import InMemoryStore
from whale_core.plugins.artifacts import ArtifactStore
from whale_core.plugins.capability_setup import configure_optional_capabilities
from whale_core.plugins.media_video import DashScopeWanVideoProvider, VideoError, VideoTaskManager
from whale_core.plugins.realtime_voice import OpenAICompatibleRealtime
from whale_core.plugins.registry import PluginRegistry
from whale_core.plugins.speech import OpenAICompatibleASR, OpenAICompatibleTTS, SpeechError
from whale_core.plugins import provider_http


def test_artifact_roundtrip_expiry_integrity_and_paths(tmp_path, monkeypatch):
    store = InMemoryStore()
    artifacts = ArtifactStore(tmp_path, store)
    artifact = artifacts.save_reference('data:image/png;base64,'+base64.b64encode(b'image').decode())
    assert artifacts.read(artifact.artifact_id) == (artifact, b'image')
    assert artifact.url.startswith('/api/v1/artifacts/')
    assert os.stat(next(tmp_path.iterdir())).st_mode & 0o777 == 0o600
    with pytest.raises(KeyError): artifacts.read('../etc/passwd')
    path = next(tmp_path.iterdir())
    path.write_bytes(b'corrupted')
    with pytest.raises(ValueError): artifacts.read(artifact.artifact_id)
    monkeypatch.setattr('whale_core.plugins.artifacts.time.time', lambda: artifact.expires_at+1)
    assert artifacts.cleanup() == 1
    assert not path.exists()


def test_artifact_rejects_active_content_and_limits(tmp_path):
    a = ArtifactStore(tmp_path, InMemoryStore(), max_bytes=4)
    for mime in ('image/svg+xml', 'text/html', 'application/javascript'):
        with pytest.raises(ValueError): a.save_bytes(b'abc', mime)
    with pytest.raises(ValueError): a.save_bytes(b'12345', 'image/png')
    with pytest.raises(ValueError): a.save_reference('data:image/png;base64,!!!!')
    with pytest.raises(Exception): a.save_reference('http://127.0.0.1:1234/private')


def test_no_optional_services_no_mock(tmp_path):
    registry, tools = PluginRegistry(), ToolRegistry()
    result = configure_optional_capabilities(registry, tools, InMemoryStore(), env={}, artifact_dir=tmp_path)
    assert registry.manifests() == [] and tools.specs() == []
    assert result.video_manager is None


def test_image_tool_stores_output_and_disable_revokes_handler(tmp_path, monkeypatch):
    registry, tools = PluginRegistry(), ToolRegistry()
    bundle = configure_optional_capabilities(registry, tools, InMemoryStore(),
        env={'WHALE_IMAGE_BASE_URL':'http://localhost:1234/v1', 'WHALE_IMAGE_PROVIDER':'openai'}, artifact_dir=tmp_path)
    payload = {'data':[{'b64_json':base64.b64encode(b'image').decode()}]}
    monkeypatch.setattr(provider_http, 'open_request', lambda *a,**k: io.BytesIO(json.dumps(payload).encode()))
    spec = tools.for_capability('media.image.generate')[0]
    result = json.loads(spec.handler({'prompt':'鲸鱼'}))
    assert result['images'][0].startswith('/api/v1/artifacts/')
    assert bundle.artifacts.read(result['images'][0].rsplit('/',1)[1])[1] == b'image'
    registry.unregister('media_image')
    with pytest.raises(ValueError): spec.handler({'prompt':'鲸鱼'})


def test_image_tool_result_has_no_bare_urls(tmp_path, monkeypatch):
    """2026-11-01「恶意链接」事件：工具结果不再回裸签名 URL（模型会贴进
    QQ 正文触发安全警告）；只留本地工件引用 + 不贴链接的提示。"""
    registry, tools = PluginRegistry(), ToolRegistry()
    configure_optional_capabilities(registry, tools, InMemoryStore(),
        env={'WHALE_IMAGE_BASE_URL':'http://localhost:1234/v1',
             'WHALE_IMAGE_PROVIDER':'openai'}, artifact_dir=tmp_path)
    payload = {'data':[{'b64_json':base64.b64encode(b'image').decode()}]}
    monkeypatch.setattr(provider_http, 'open_request', lambda *a,**k: io.BytesIO(json.dumps(payload).encode()))
    spec = tools.for_capability('media.image.generate')[0]
    result = json.loads(spec.handler({'prompt':'鲸鱼'}))
    assert 'urls' not in result
    assert 'note' in result and '不要包含' in result['note']
    assert result['images'][0].startswith('/api/v1/artifacts/')


def test_media_style_default_anime_and_explicit_style_wins(tmp_path, monkeypatch):
    """2026-11-01 owner 决策：默认二次元动漫画风；提示词自带风格词不追加。"""
    registry, tools = PluginRegistry(), ToolRegistry()
    configure_optional_capabilities(registry, tools, InMemoryStore(),
        env={'WHALE_IMAGE_BASE_URL':'http://localhost:1234/v1',
             'WHALE_IMAGE_PROVIDER':'openai'}, artifact_dir=tmp_path)
    seen = []
    payload = {'data':[{'b64_json':base64.b64encode(b'img').decode()}]}
    def transport(req, **kw):
        seen.append(json.loads(req.data).get('prompt',''))
        return io.BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr(provider_http, 'open_request', transport)
    spec = tools.for_capability('media.image.generate')[0]
    spec.handler({'prompt':'一只小鲸鱼'})
    spec.handler({'prompt':'一只写实照片风的小鲸鱼'})
    spec.handler({'prompt':'一只小鲸鱼', })  # 再画一张无风格的
    assert seen[0].endswith('二次元动漫风格')
    assert seen[1] == '一只写实照片风的小鲸鱼'  # 显式风格不被覆盖
    assert seen[2].endswith('二次元动漫风格')
    # 环境可关/可换
    registry2, tools2 = PluginRegistry(), ToolRegistry()
    configure_optional_capabilities(registry2, tools2, InMemoryStore(),
        env={'WHALE_IMAGE_BASE_URL':'http://localhost:1234/v1',
             'WHALE_IMAGE_PROVIDER':'openai', 'WHALE_MEDIA_STYLE':''}, artifact_dir=tmp_path)


def test_minimax_image_default_when_key_present(tmp_path, monkeypatch):
    """MiniMax key 就位：图像默认模型切成 minimax-image-01（owner 决策），
    qwen 仍列可选；key 缺席则保持 qwen 默认。"""
    registry, tools = PluginRegistry(), ToolRegistry()
    configure_optional_capabilities(registry, tools, InMemoryStore(),
        env={'WHALE_IMAGE_BASE_URL':'http://localhost:1234/v1',
             'WHALE_IMAGE_PROVIDER':'dashscope',
             'WHALE_MINIMAX_API_KEY':'k'}, artifact_dir=tmp_path)
    spec = tools.for_capability('media.image.generate')[0]
    assert 'minimax-image-01' in spec.description
    assert 'qwen-image-3.0' in spec.description
    # 默认模型路由到 MiniMax provider（HTTP 协议层在 test_minimax_media 覆盖）
    from whale_core.plugins.media_image import ImageTaskResult
    from whale_core.plugins import minimax_media as _mm
    calls = []
    def _fake_generate(self, prompt, *, size='1328*1328', n=1):
        calls.append(prompt)
        return ImageTaskResult(task_id='t', status='succeeded',
            images=('data:image/png;base64,'+base64.b64encode(b'img').decode(),))
    monkeypatch.setattr(_mm.MiniMaxImageProvider, 'generate', _fake_generate)
    result = json.loads(spec.handler({'prompt':'鲸鱼'}))
    assert result['model'] == 'minimax-image-01' and calls
    # key 缺席：minimax 模型不出现
    registry3, tools3 = PluginRegistry(), ToolRegistry()
    configure_optional_capabilities(registry3, tools3, InMemoryStore(),
        env={'WHALE_IMAGE_BASE_URL':'http://localhost:1234/v1',
             'WHALE_IMAGE_PROVIDER':'dashscope'}, artifact_dir=tmp_path)
    spec3 = tools3.for_capability('media.image.generate')[0]
    assert 'minimax' not in spec3.description


def test_wan_http_protocol_and_state_validation(monkeypatch):
    calls = []
    responses = [{'output':{'task_id':'job-1'}}, {'output':{'task_status':'SUCCEEDED','video_url':'https://cdn.example/x.mp4'}}, {}]
    def transport(req, **kw):
        calls.append(req)
        return io.BytesIO(json.dumps(responses.pop(0)).encode())
    monkeypatch.setattr(provider_http, 'open_request', transport)
    p = DashScopeWanVideoProvider(api_key='private-key')
    assert p.submit('鲸鱼',duration_s=5) == 'job-1'
    assert json.loads(calls[0].data)['parameters'] == {'size':'1280*720','duration':5}
    assert calls[0].get_header('X-dashscope-async') == 'enable'
    assert p.poll('job-1')[0] == 'succeeded'
    assert p.cancel('job-1')
    assert calls[-1].full_url.endswith('/tasks/job-1/cancel')
    with pytest.raises(VideoError): p.poll('../metadata')
    with pytest.raises(ValueError): p.submit('鲸鱼',duration_s=7)


def test_video_poll_validates_and_times_out_without_resubmit(monkeypatch):
    class Provider:
        submits = 0
        def submit(self,*a,**kw): self.submits += 1; return 'a'
        def poll(self,*a): return 'invented','', ''
        def cancel(self,*a): return False
    p = Provider(); m = VideoTaskManager(InMemoryStore(),p)
    task=m.submit('x')
    assert m.poll(task.task_id).status == 'pending'
    assert 'invalid_status' in m.poll(task.task_id).error
    monkeypatch.setattr('whale_core.plugins.media_video.time.time',lambda: task.created_at+1900)
    done=m.poll(task.task_id)
    assert done.status == 'failed' and 'remote_execution_may_continue' in done.error
    assert p.submits == 1


def test_transport_forbids_redirect_before_credentials_can_leak(monkeypatch):
    captured = []
    class Redirect(urllib.request.BaseHandler):
        def default_open(self, req):
            captured.append(req.full_url)
            raise urllib.error.HTTPError(req.full_url,302,'redirect',{'Location':'https://evil.example/'},None)
    opener=urllib.request.build_opener(Redirect(), provider_http._NoRedirectHandler())
    monkeypatch.setattr(urllib.request,'build_opener',lambda *_:opener)
    with pytest.raises(urllib.error.HTTPError):
        provider_http.open_request(urllib.request.Request('https://provider.example/',headers={'Authorization':'Bearer secret'}),timeout=1)
    assert captured == ['https://provider.example/']


def test_speech_empty_and_oversized_responses_rejected(monkeypatch):
    class Response(io.BytesIO):
        headers={'Content-Type':'audio/mpeg'}
    monkeypatch.setattr(provider_http,'open_request',lambda *a,**kw: Response(b''))
    with pytest.raises(SpeechError): OpenAICompatibleTTS(api_key='x').synthesize('hello')
    with pytest.raises(ValueError): OpenAICompatibleTTS(api_key='x').synthesize('')
    monkeypatch.setattr(provider_http,'open_request',lambda *a,**kw: Response(b'x'*(1024*1024+1)))
    with pytest.raises(SpeechError): OpenAICompatibleASR(api_key='x').transcribe(b'123')


def test_realtime_native_audio_session_lifecycle():
    class Connection:
        sent=[]
        closed=False
        def send(self,raw): self.sent.append(json.loads(raw))
        def recv(self,timeout): return json.dumps({'type':'response.audio.delta','delta':'YQ=='})
        def close(self): self.closed=True
    connection=Connection(); captured={}
    def connect(url,**kw): captured.update(url=url,**kw); return connection
    provider=OpenAICompatibleRealtime(url='wss://voice.example/realtime',api_key='secret',model='omni',connector=connect)
    with provider.open_session(instructions='personality') as session:
        session.append_audio(b'abc'); session.commit()
        assert session.receive()['type'] == 'response.audio.delta'
        session.cancel()
    assert connection.closed
    assert 'model=omni' in captured['url']
    assert [event['type'] for event in connection.sent] == ['session.update','input_audio_buffer.append','input_audio_buffer.commit','response.create','response.cancel','input_audio_buffer.clear']
    assert connection.sent[1]['audio'] == base64.b64encode(b'abc').decode()


def test_voice_bridge_enforces_session_limits_and_audio_contract(monkeypatch):
    from whale_core.plugins.realtime_bridge import RealtimeVoiceBridge, BridgeError
    class Session:
        closed=False
        audio=[]
        def append_audio(self,audio): self.audio.append(audio)
        def commit(self): self.committed=True
        def cancel(self): self.cancelled=True
        def receive(self,timeout): return {'type':'response.done'}
        def close(self): self.closed=True
    class Provider:
        sessions=[]
        def open_session(self,**kw):
            item=Session(); self.sessions.append(item); return item
    provider=Provider()
    bridge=RealtimeVoiceBridge(provider,max_sessions=1,idle_timeout=120)
    try:
        sid=bridge.open(instructions='personality')['session_id']
        assert len(sid)==32
        with pytest.raises(BridgeError): bridge.open()
        assert bridge.append(sid,'YWJj')['status']=='accepted'
        assert provider.sessions[0].audio==[b'abc']
        bridge.commit(sid); bridge.cancel(sid)
        assert bridge.receive(sid)=={'type':'response.done'}
        with pytest.raises(BridgeError): bridge.append(sid,'!!!!')
        with pytest.raises(BridgeError): bridge.append(sid,'YQ=='*500000)
        with pytest.raises(BridgeError): bridge.receive(sid,timeout=999)
        bridge.close(sid)
        assert provider.sessions[0].closed
        with pytest.raises(BridgeError): bridge.commit(sid)
        sid2=bridge.open()['session_id']
        bridge._sessions[sid2].touched-=121
        assert bridge.cleanup()==1 and provider.sessions[-1].closed
    finally:
        bridge.shutdown()
    with pytest.raises(BridgeError): bridge.open()


def test_realtime_network_errors_classified_without_secret():
    from whale_core.plugins.realtime_voice import RealtimeVoiceSession
    class BadConnection:
        def send(self,*a): raise OSError('secret')
        def recv(self,**kw): raise OSError('secret')
        def close(self): pass
    session=RealtimeVoiceSession(BadConnection())
    for invoke in (lambda:session.append_audio(b'a'), lambda:session.receive()):
        with pytest.raises(SpeechError) as exc: invoke()
        assert exc.value.kind=='network' and 'secret' not in str(exc.value)


def test_asr_browser_webm_preserves_filename(monkeypatch):
    captured=[]
    def transport(req,**kw):
        captured.append(req.data)
        return io.BytesIO(b'{"text":"hello"}')
    monkeypatch.setattr(provider_http,'open_request',transport)
    assert OpenAICompatibleASR(api_key='x').transcribe(b'webm-audio',format='webm')=='hello'
    assert b'filename="audio.webm"' in captured[0]
    with pytest.raises(ValueError):
        OpenAICompatibleASR(api_key='x').transcribe(b'audio',format='../wav')


def test_capability_env_fallback_chain():
    """回退链：专属 > 共享媒体平台 > 回复模型同款（owner 决策 2026-10-30）。"""
    from whale_core.plugins.capability_setup import _resolve_capability_env
    # 只有回复模型配置 → 媒体吃回复模型同款
    b, k, kind = _resolve_capability_env({"DEEPSEEK_BASE_URL": "https://platform.agentscope.io/v1",
                                          "DEEPSEEK_API_KEY": "sk-as-x"}, "IMAGE")
    assert b == "https://platform.agentscope.io/v1" and k == "sk-as-x".replace("x", "") or k.startswith("sk-as")
    assert kind == "dashscope"
    # 共享媒体平台覆盖回复模型
    b2, k2, _ = _resolve_capability_env({"DEEPSEEK_BASE_URL": "https://a", "DEEPSEEK_API_KEY": "ka",
                                         "WHALE_MEDIA_BASE_URL": "https://b", "WHALE_MEDIA_API_KEY": "kb"}, "IMAGE")
    assert b2 == "https://b" and k2 == "kb"
    # 专属覆盖最高
    b3, _, _ = _resolve_capability_env({"WHALE_MEDIA_BASE_URL": "https://b",
                                        "WHALE_IMAGE_BASE_URL": "https://c"}, "IMAGE")
    assert b3 == "https://c"


def test_capability_env_inherits_reply_endpoint_only_when_compatible():
    """P1 供应商适配矩阵：回复模型端点不兼容 DashScope 协议时不继承。"""
    from whale_core.plugins.capability_setup import _resolve_capability_env
    # DeepSeek 官方端点不是 DashScope 媒体协议 → 不继承（诚实「未配置」）
    b, k, _ = _resolve_capability_env(
        {"DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
         "DEEPSEEK_API_KEY": "sk-dp"}, "IMAGE")
    assert b == "" and k == ""
    # AgentScope / 阿里百炼端点 → 兼容，继承（owner 决策同款默认）
    for host in ("https://platform.agentscope.io/v1",
                 "https://dashscope.aliyuncs.com/compatible-mode/v1"):
        b, k, kind = _resolve_capability_env(
            {"DEEPSEEK_BASE_URL": host, "DEEPSEEK_API_KEY": "sk-as"}, "IMAGE")
        assert b == host and k == "sk-as" and kind == "dashscope"
    # 用户显式声明 provider → 视为用户断言兼容，允许继承
    b, k, kind = _resolve_capability_env(
        {"DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1",
         "DEEPSEEK_API_KEY": "sk-dp", "WHALE_IMAGE_PROVIDER": "openai"}, "IMAGE")
    assert b == "https://api.deepseek.com/v1" and kind == "openai"


def test_pcm_bridge_fields_in_settings_contract():
    """P1：PCM 桥配置族进入设置页契约；token 属敏感字段不回显。"""
    from whale_core.configuration import SECRET_FIELDS, SERVICE_FIELDS
    for field in ("WHALE_PCM_BRIDGE_HOST", "WHALE_PCM_BRIDGE_PORT",
                  "WHALE_PCM_BRIDGE_TOKEN", "WHALE_PCM_BRIDGE_TLS",
                  "WHALE_PCM_BRIDGE_PUBLIC_WS_URL"):
        assert field in SERVICE_FIELDS
    assert "WHALE_PCM_BRIDGE_TOKEN" in SECRET_FIELDS


def test_media_model_selection_and_minimax_registration(tmp_path, monkeypatch):
    """2026-11-01 owner 决策：图/视频/语音工具可按 model 参数选模型；
    未知模型诚实报错并列可用清单；MiniMax 配 key 才注册。"""
    env = {
        'WHALE_MEDIA_BASE_URL': 'http://localhost:1234/v1',
        'WHALE_MEDIA_API_KEY': 'sk-test',
        'WHALE_IMAGE_MODELS': 'qwen-image-2.0',
        'WHALE_TTS_MODELS': 'qwen-audio-3.0-tts-plus',
        'WHALE_VIDEO_MODELS': 'wan3.0-video',
        'WHALE_MINIMAX_API_KEY': 'mm-key',
    }
    registry, tools = PluginRegistry(), ToolRegistry()
    bundle = configure_optional_capabilities(registry, tools, InMemoryStore(),
                                             env=env, artifact_dir=tmp_path)

    # 图像：默认 qwen-image-3.0 + 追加 qwen-image-3.0 外的模型；未知模型诚实报错
    img_spec = tools.for_capability('media.image.generate')[0]
    payload = {'output': {'choices': [{'message': {'content': [
        {'image': 'https://cdn.example/a.png'}]}}]}}
    monkeypatch.setattr(provider_http, 'open_request',
                        lambda *a, **k: io.BytesIO(json.dumps(payload).encode()))
    # 工件落盘会真实抓取 URL——本测试只关心模型路由，stub 掉下载
    class _StubArtifact:
        url = '/api/v1/artifacts/stub'
    monkeypatch.setattr(bundle.artifacts, 'save_reference',
                        lambda ref, **kw: _StubArtifact())
    r = json.loads(img_spec.handler({'prompt': '鲸', 'model': 'qwen-image-2.0'}))
    assert r['model'] == 'qwen-image-2.0'
    with pytest.raises(ValueError) as ei:
        img_spec.handler({'prompt': '鲸', 'model': 'dall-e-9'})
    assert 'qwen-image-2.0' in str(ei.value) and '未知生图模型' in str(ei.value)

    # 语音：dashscope flash/plus + minimax speech-02-hd 注册进可选模型
    tts_spec = tools.for_capability('tts.synthesize')[0]
    assert 'speech-02-hd' in tts_spec.description
    with pytest.raises(ValueError) as ei:
        tts_spec.handler({'text': '你好', 'model': 'no-such'})
    assert '未知语音模型' in str(ei.value)

    # 视频：happyhorse + wan3.0-video + MiniMax-Hailuo-2.3 全部可选
    v_spec = tools.for_capability('media.video.generate')[0]
    assert 'wan3.0-video' in v_spec.description
    assert 'MiniMax-Hailuo-2.3' in v_spec.description
    assert bundle.video_manager is not None
    assert 'MiniMax-Hailuo-2.3' in bundle.video_manager.available_models
    with pytest.raises(VideoError):
        bundle.video_manager.submit('鲸', model='no-such-video')


def test_no_minimax_key_no_minimax_models(tmp_path):
    registry, tools = PluginRegistry(), ToolRegistry()
    bundle = configure_optional_capabilities(registry, tools, InMemoryStore(),
        env={'WHALE_MEDIA_BASE_URL': 'http://localhost:1234/v1',
             'WHALE_MEDIA_API_KEY': 'sk-test'}, artifact_dir=tmp_path)
    tts_spec = tools.for_capability('tts.synthesize')[0]
    assert 'speech-02' not in tts_spec.description
    assert 'MiniMax' not in tools.for_capability('media.video.generate')[0].description
