import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, api, apiUrl, isUnauthorized, setToken } from './api';

/** 构造最小可用的 Response mock。 */
function mockResponse(
  status: number,
  body: unknown,
  opts: { json?: boolean } = {},
): Response {
  const isJson = opts.json !== false;
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: `Status ${status}`,
    json: async () => {
      if (!isJson) throw new Error('not json');
      return body;
    },
    text: async () => (typeof body === 'string' ? body : JSON.stringify(body)),
    headers: new Headers(),
  } as unknown as Response;
}

function stubFetch(res: Response) {
  const m = vi.fn().mockResolvedValue(res);
  vi.stubGlobal('fetch', m);
  return m;
}

describe('api client', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    setToken('');
  });

  it('拼接 /api/v1 前缀并在请求头携带 Bearer token', async () => {
    const fetchMock = stubFetch(mockResponse(200, { result: { ok: true } }));
    setToken('secret-token');
    const r = await api.health();
    expect(r.ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/v1/health');
    expect((init.headers as Headers).get('Authorization')).toBe('Bearer secret-token');
  });

  it('无 token 时不发送 Authorization 头', async () => {
    const fetchMock = stubFetch(mockResponse(200, { result: { ok: true } }));
    await api.health();
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect((init.headers as Headers).has('Authorization')).toBe(false);
  });

  it('401 时按 {error:{code,message}} 抛 ApiError 且 isUnauthorized 为真', async () => {
    stubFetch(
      mockResponse(401, { error: { code: 'unauthorized', message: 'bad token' } }),
    );
    const err: unknown = await api.status().catch(e => e);
    expect(err).toBeInstanceOf(ApiError);
    const apiErr = err as ApiError;
    expect(apiErr.status).toBe(401);
    expect(apiErr.code).toBe('unauthorized');
    expect(apiErr.message).toBe('bad token');
    expect(isUnauthorized(apiErr)).toBe(true);
  });

  it('错误体非 JSON 时回退为 http_<status>', async () => {
    stubFetch(mockResponse(502, 'Bad Gateway', { json: false }));
    const err: unknown = await api.status().catch(e => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).code).toBe('http_502');
    expect(isUnauthorized(err)).toBe(false);
  });

  it('记忆 key 经 encodeURIComponent 编码', async () => {
    const fetchMock = stubFetch(mockResponse(200, { result: { deleted: true } }));
    const r = await api.deleteMemory('life:draft:a/b c');
    expect(r.deleted).toBe(true);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`/api/v1/memory/${encodeURIComponent('life:draft:a/b c')}`);
    expect(url).toBe('/api/v1/memory/life%3Adraft%3Aa%2Fb%20c');
    expect(init.method).toBe('DELETE');
  });

  it('chat 以 POST 发送 conversation_id 与 text', async () => {
    const fetchMock = stubFetch(
      mockResponse(200, { result: { reply: 'hi', status: 'ok', receipts: [] } }),
    );
    await api.chat('你好');
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/v1/chat');
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({ conversation_id: 'console', text: '你好' });
    expect((init.headers as Headers).get('Content-Type')).toBe('application/json');
  });

  it('apiUrl 为相对路径补前导斜杠', () => {
    expect(apiUrl('/status')).toBe('/api/v1/status');
    expect(apiUrl('status')).toBe('/api/v1/status');
  });
});
