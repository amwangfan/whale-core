# 贡献指南

感谢对 whale-core 的兴趣。本仓是陪伴型 AI 的智能层核心库：**纯 Python stdlib、
零第三方依赖、全部测试离线可跑**——请在改动时守住这三条底线。

## 开发流程

```bash
pip install -e ".[dev]"   # 或 uv sync --extra dev
pytest -q                 # 全量测试（离线，不触网）
```

- **小步提交**：一个 PR/提交解决一个可验收目标；不要把重构与功能混在一起。
- **测试先行**：每个功能带测试；安全相关路径（鉴权/门控/脱敏/删除语义）
  必须有负向测试。
- **诚实状态**：文档区分「已测试 / 已有代码 / 已联通（真实服务联调）/ stub /
  规划」；stub 不冒充完成。
- **契约先行**：新能力先定契约（事件/意图/Capability schema + 错误格式），
  再接具体 provider。

## 硬边界（不可协商）

1. 密钥/Token/真实设备地址永不进仓库、日志、异常文本、manifest；
2. 插件不提供任意 shell 等价物（命令执行只有 `cloud.shell_policy` 分档 +
   `cloud.shell_executor` 一个入口）；
3. 身份与审批不可自升：插件事件/意图不携带 owner/审批声明，由宿主可信
   verifier 裁决；
4. 交易级动作永远逐笔人审 + 限额；
5. 原始音频字节不入库；私人转写不进普通日志；
6. 记忆可导出、可删除（删除是真删，不是界面隐藏）。

## 第三方素材

引入第三方素材（人格卡、图标、模型配置）必须确认许可证并把文本放进
`licenses/`；许可不明的只存元数据与来源链接（参照 `ui/persona_cards.py`
的 aigengtu 目录做法）。

## 目录速览

- `src/whale_core/`：核心库（core/memory/perception/initiative/actions/cloud/
  life/ui/plugins/api/cli/skills/profiles）
- `web/`：网页控制台（React + Vite；`pnpm -C web build/test`）
- `examples/`：离线演示；`tests/`：pytest；`docs/`：契约与规划文档
- CI：`.github/workflows/tests.yml`（pytest）+ `web-build.yml`（前端构建）
