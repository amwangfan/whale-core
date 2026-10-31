# G3 三条全天助手场景——验收记录（模拟器层）

- 日期：2026-10-31
- 范围确认（owner）：本轮只做**模拟器层**验证；真机/真实服务验收记入遗留清单。
- 自动化证据：`tests/test_g3_scenarios.py`（10 例，全绿，离线）。

## 场景 1：上课与天气

- 实现：`src/whale_core/life/day_context.py`
  - `DayPlanStore`：日程事件必须带可信 `source`（空来源拒绝入库——陌生事件须过来源检查）；
  - `WeatherProvider` 协议：真实实现经 web.fetch/供应商 API（本轮为测试替身）；
  - `ContextReminder.evaluate_departure`：规则触发（不调模型）→ 4 小时窗内最近事件 + 天气快照 → 仅在「有雨」时产出带伞草稿；草稿 evidence 同时含事件来源与天气来源（可点击、可追溯）；走既有 Outbox 策略闸门（enabled/渠道/静默时段/日额度/24h 主题去重），可 revoke、可整体关闭。
- 模拟器验证（7 例）：有课+下雨→带来源草稿；晴天→零产出；静默时段→deferred 不派发；prefs 关闭→零产出；无课→零产出；天气不可用→诚实不提醒（不猜）；同事件 24h 去重。
- **真机缺口**：真实日历/天气 provider 接入、手机/眼镜端提醒通道实发、误报率观察。

## 场景 2：慢任务与结果交付（QQ 带图再创作）

- 链路：QQ 入站（图片附件过 G0-B 白名单验证器）→ 两段式 ack 草稿 → 后台任务执行 → 完成补发（completion_sender）→ `mark_delivered` 送达回执 → `tasks_for_owner` 跨渠道进度可见。
- 模拟器验证（2 例）：成功路径补发含真实图片链接且 phase=delivered；失败路径补发为诚实失败口径（无「成功」假象）且 phase=failed 可见。
- **真机缺口**：真实 QQ 消息往返回归（owner 侧实测）；weq 发送失败的 `delivery_failed` 人工重发演练。

## 场景 3：连续电话/语音

- 实现要点复用：`OwnerRegistry.bind_session`（scope 授权）+ `tasks_for_owner`（进度查询不含正文）+ `RealtimeTranscriptRecorder`（只落文本转写，从不存音频）+ 挂断 `revoke_session`。
- 模拟器验证（1 例）：scope 内可查进度、scope 外不可得；转写只产生文本 episode（store 无音频字节键）；挂断后会话绑定失效。
- **真机缺口**：whale-io 桥真机联调（G0 已完成协议镜像层）；挂断时 Omni 侧 session/媒体资源释放的端到端确认。

## 遗留（交给后续对话/真机窗口）

1. 三条场景的真机验收记录（环境、时间、延迟、可否打断、失败注入）。
2. 天气/日历真实 provider 插件（经 web.fetch + 来源核验）。
3. 提醒通道的「眼镜只收允许的提醒」端侧实发验证。
