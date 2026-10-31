import { useState, type FormEvent } from "react";
import { api, type RuntimeSettings } from "../api";
import Icon from "./Icon";

type Step = "model" | "extras" | "ready";
type Profile = RuntimeSettings["profile"];

const PRESETS: Record<Profile, { url: string; model: string }> = {
  cloud: { url: "https://api.deepseek.com", model: "deepseek-chat" },
  local: { url: "http://127.0.0.1:11434/v1", model: "local-model" },
};

function configuredService(settings: RuntimeSettings, name: string): string {
  const value = settings.services?.[name];
  return typeof value === "string" ? value : "";
}

export default function FirstRunSetup({
  initial,
  onComplete,
}: {
  initial: RuntimeSettings;
  onComplete: () => void;
}) {
  const [step, setStep] = useState<Step>("model");
  const [profile, setProfile] = useState<Profile>(initial.profile);
  const [baseUrl, setBaseUrl] = useState(initial.model.base_url);
  const [modelName, setModelName] = useState(initial.model.model);
  const [apiKey, setApiKey] = useState("");
  const [intentEnabled, setIntentEnabled] = useState(initial.intent_model_enabled);
  const [searchProvider, setSearchProvider] = useState(
    configuredService(initial, "WHALE_SEARCH_PROVIDER"),
  );
  const [searchKey, setSearchKey] = useState("");
  const [imageUrl, setImageUrl] = useState(
    configuredService(initial, "WHALE_IMAGE_BASE_URL"),
  );
  const [imageKey, setImageKey] = useState("");
  const [imageModel, setImageModel] = useState(
    configuredService(initial, "WHALE_IMAGE_MODEL"),
  );
  const [realtimeUrl, setRealtimeUrl] = useState(
    configuredService(initial, "WHALE_REALTIME_URL"),
  );
  const [realtimeKey, setRealtimeKey] = useState("");
  const [modelCalls, setModelCalls] = useState(
    initial.budget?.limits.model_calls ?? 200,
  );
  const [mediaJobs, setMediaJobs] = useState(
    initial.budget?.limits.media_jobs ?? 50,
  );
  const [working, setWorking] = useState(false);
  const [error, setError] = useState("");
  const [testResult, setTestResult] = useState("");

  const chooseProfile = (next: Profile) => {
    if (next === profile) return;
    setProfile(next);
    setBaseUrl(PRESETS[next].url);
    setModelName(PRESETS[next].model);
    setApiKey("");
  };

  const cloudNeedsKey =
    profile === "cloud" &&
    !apiKey.trim() &&
    !(initial.profile === "cloud" && initial.model.api_key_configured);

  const validBudget = (value: number) =>
    Number.isInteger(value) && value >= 0 && value <= 100000;

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (cloudNeedsKey) {
      setStep("model");
      setError("云端模型需要 API Key。若已通过环境变量配置，请确认服务是否读取到了密钥。");
      return;
    }
    if (!validBudget(modelCalls) || !validBudget(mediaJobs)) {
      setError("每日预算必须是 0–100000 的整数。");
      return;
    }
    setWorking(true);
    setError("");
    try {
      const services: Record<string, string> = {};
      if (searchProvider) services.WHALE_SEARCH_PROVIDER = searchProvider;
      if (searchKey.trim()) {
        services[searchProvider === "brave" ? "BRAVE_API_KEY" : "TAVILY_API_KEY"] =
          searchKey.trim();
      }
      if (imageUrl.trim()) services.WHALE_IMAGE_BASE_URL = imageUrl.trim();
      if (imageModel.trim()) services.WHALE_IMAGE_MODEL = imageModel.trim();
      if (imageKey.trim()) services.WHALE_IMAGE_API_KEY = imageKey.trim();
      if (realtimeUrl.trim()) services.WHALE_REALTIME_URL = realtimeUrl.trim();
      if (realtimeKey.trim()) services.WHALE_REALTIME_API_KEY = realtimeKey.trim();

      await api.saveSettings({
        setup_completed: true,
        profile,
        model: {
          base_url: baseUrl.trim(),
          model: modelName.trim(),
          ...(apiKey.trim() ? { api_key: apiKey.trim() } : {}),
        },
        intent_model_enabled: intentEnabled,
        services,
        budget: { model_calls: modelCalls, media_jobs: mediaJobs },
      });
      setApiKey("");
      setSearchKey("");
      setImageKey("");
      setRealtimeKey("");
      setStep("ready");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "保存设置失败，请检查填写内容。");
    } finally {
      setWorking(false);
    }
  };

  const skip = async () => {
    setWorking(true);
    setError("");
    try {
      await api.saveSettings({ setup_completed: true });
      onComplete();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "无法保存引导状态，请稍后重试。");
    } finally {
      setWorking(false);
    }
  };

  const test = async () => {
    setWorking(true);
    setError("");
    setTestResult("");
    try {
      const result = await api.testProvider();
      setTestResult(
        result.ok
          ? "连接成功，可以开始使用鲸鱼。"
          : "连接尚未成功：" + (result.error_kind ?? "模型未返回有效结果") +
              "。可返回修改或稍后在设置中排查。",
      );
    } catch (cause) {
      setTestResult(
        "连接测试未完成：" +
          (cause instanceof Error ? cause.message : "网络错误") +
          "。已经保存的配置不会丢失。",
      );
    } finally {
      setWorking(false);
    }
  };

  return (
    <div className="setup-screen">
      <div className="setup-shell">
        <header className="setup-header">
          <div className="wordmark">
            <span className="logo"><Icon name="whale" size={25} /></span>
            WHALE <span className="wordmark-small">CORE</span>
          </div>
          <span className="setup-counter">
            首次配置 · {step === "model" ? "1" : step === "extras" ? "2" : "3"} / 3
          </span>
        </header>
        <main className="setup-panel">
          <p className="eyebrow">GETTING STARTED</p>
          <h1>{step === "model" ? "先连接你的 AI 模型" :
            step === "extras" ? "再选择需要的能力" : "鲸鱼已经准备好了"}</h1>
          <p className="setup-intro">
            {step === "model"
              ? "设置主模型、接口地址和 API Key。已有配置会自动带入，密钥不会显示在页面上。"
              : step === "extras"
                ? "搜索、生成和实时语音均可选。这里不需要一次填完，之后仍可随时修改。"
                : "配置已保存到服务端。可以测试模型连接，也可以直接进入工作台。"}
          </p>

          {error && <div className="banner error" role="alert">{error}</div>}
          {step !== "ready" ? (
            <form onSubmit={step === "model" ? (event) => {
              event.preventDefault();
              if (!baseUrl.trim() || !modelName.trim() || cloudNeedsKey) {
                setError("请填写模型端点与名称；云端模型还需要 API Key。");
                return;
              }
              setError("");
              setStep("extras");
            } : save}>
              {step === "model" ? (
                <div className="setup-fields">
                  <div className="form-field">
                    <label htmlFor="setup-profile">运行方式</label>
                    <select id="setup-profile" value={profile}
                      onChange={(e) => chooseProfile(e.target.value as Profile)}>
                      <option value="cloud">云端模型（OpenAI 兼容）</option>
                      <option value="local">本地模型（OpenAI 兼容）</option>
                    </select>
                  </div>
                  <div className="form-field">
                    <label htmlFor="setup-url">API 接口地址</label>
                    <input id="setup-url" type="url" required value={baseUrl}
                      placeholder="https://api.deepseek.com"
                      onChange={(e) => setBaseUrl(e.target.value)} />
                    <small>使用服务商提供的 API Base URL；本地服务允许本机或私网 HTTP 地址。</small>
                  </div>
                  <div className="form-field">
                    <label htmlFor="setup-model">模型名称</label>
                    <input id="setup-model" required value={modelName}
                      placeholder="deepseek-chat"
                      onChange={(e) => setModelName(e.target.value)} />
                  </div>
                  <div className="form-field">
                    <label htmlFor="setup-key">API Key（{profile === "local" ? "可选" : "必填"}）</label>
                    <input id="setup-key" type="password" value={apiKey}
                      autoComplete="new-password" spellCheck={false}
                      placeholder={initial.profile === profile && initial.model.api_key_configured
                        ? "已配置；留空保留密钥" : "填写模型服务商的 API Key"}
                      onChange={(e) => setApiKey(e.target.value)} />
                    <small>仅随受认证的设置请求提交到核心；不会写入浏览器本地存储或回显密钥。</small>
                  </div>
                </div>
              ) : (
                <div className="setup-fields">
                  <label className="check-row">
                    <input type="checkbox" checked={intentEnabled}
                      onChange={(e) => setIntentEnabled(e.target.checked)} />
                    启用模型意图规划（需要额外的模型调用）
                  </label>
                  <div className="form-field">
                    <label htmlFor="setup-search">搜索服务（可选）</label>
                    <select id="setup-search" value={searchProvider}
                      onChange={(e) => setSearchProvider(e.target.value)}>
                      <option value="">暂不配置</option>
                      <option value="tavily">Tavily</option>
                      <option value="brave">Brave</option>
                      <option value="ddg">DuckDuckGo（无需 API Key）</option>
                    </select>
                    {searchProvider !== "" && searchProvider !== "ddg" && (
                      <input aria-label="搜索 API Key" type="password" value={searchKey}
                        autoComplete="new-password" placeholder="搜索 API Key（留空保留已有配置）"
                        onChange={(e) => setSearchKey(e.target.value)} />
                    )}
                  </div>
                  <details className="service-group">
                    <summary>图片生成（可选）</summary>
                    <div className="setup-fields">
                      <label className="form-field">图片 API 端点
                        <input type="url" value={imageUrl} placeholder="https://…"
                          onChange={(e) => setImageUrl(e.target.value)} />
                      </label>
                      <label className="form-field">图片模型名称
                        <input value={imageModel} onChange={(e) => setImageModel(e.target.value)} />
                      </label>
                      <label className="form-field">图片 API Key
                        <input type="password" value={imageKey} autoComplete="new-password"
                          placeholder="留空保留已有密钥" onChange={(e) => setImageKey(e.target.value)} />
                      </label>
                    </div>
                  </details>
                  <details className="service-group">
                    <summary>实时语音（可选）</summary>
                    <div className="setup-fields">
                      <label className="form-field">WebSocket 端点
                        <input type="url" value={realtimeUrl} placeholder="wss://…"
                          onChange={(e) => setRealtimeUrl(e.target.value)} />
                      </label>
                      <label className="form-field">实时语音 API Key
                        <input type="password" value={realtimeKey} autoComplete="new-password"
                          placeholder="留空保留已有密钥" onChange={(e) => setRealtimeKey(e.target.value)} />
                      </label>
                    </div>
                  </details>
                  <div className="setup-budgets">
                    <label className="form-field" htmlFor="setup-budget-model">每日模型请求预算
                      <input id="setup-budget-model" type="number" min={0} max={100000}
                        step={1} required value={modelCalls}
                        onChange={(e) => setModelCalls(e.target.valueAsNumber)} />
                    </label>
                    <label className="form-field" htmlFor="setup-budget-media">每日媒体任务预算
                      <input id="setup-budget-media" type="number" min={0} max={100000}
                        step={1} required value={mediaJobs}
                        onChange={(e) => setMediaJobs(e.target.valueAsNumber)} />
                    </label>
                  </div>
                  <p className="muted small">
                    Shell 默认保持关闭。更多分角色模型、语音识别／合成、视频与设备权限可在「设置与诊断」中配置。
                  </p>
                </div>
              )}
              <div className="setup-actions">
                {step === "extras" && (
                  <button type="button" disabled={working}
                    onClick={() => { setError(""); setStep("model"); }}>返回上一步</button>
                )}
                <button className="primary" type="submit" disabled={working}>
                  {step === "model" ? "下一步：扩展能力" :
                    working ? "正在保存配置…" : "保存配置"}
                </button>
              </div>
            </form>
          ) : (
            <div className="setup-finish">
              <div className="banner ok" role="status">设置已保存。你可以继续配置，也可以开始使用。</div>
              {testResult && <div className="banner" role="status">{testResult}</div>}
              <div className="setup-actions">
                <button type="button" disabled={working} onClick={() => void test()}>
                  {working ? "正在测试…" : "测试模型连接"}
                </button>
                <button type="button" onClick={() => setStep("model")}>修改配置</button>
                <button className="primary" type="button" disabled={working}
                  onClick={onComplete}>进入工作台</button>
              </div>
              <p className="muted small">连接测试会向模型服务商发送一次实际请求，可能产生少量费用。</p>
            </div>
          )}
          {step !== "ready" && (
            <div className="setup-footer">
              <button className="text-button" type="button" disabled={working}
                onClick={() => void skip()}>暂时跳过引导</button>
              <span className="muted small">跳过仅结束引导，不会启用任何额外权限。</span>
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
