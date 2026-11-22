import { expect, test, type Page } from '@playwright/test';
import { apiHandler, makeState, TEST_TOKEN, type FixtureState } from './fixtures';

// 全部用例经路由拦截接夹具 API（测试基建）；产品本身不含任何 mock。
// 真实服务端到端用例在本文件末尾，需 WHALE_TEST_URL / WHALE_TEST_TOKEN。

let state: FixtureState;
async function boot(page: Page, { authenticated = true } = {}) {
  state = makeState();
  state.authed = !authenticated ? false : true;
  // authed=true 的用例跳过首开流：探测直接 200
  await page.route('**/api/v1/**', apiHandler(state));
  await page.goto('/');
}

test('first-run flow: unlock with token, persist across reload, logout', async ({ page }) => {
  await boot(page, { authenticated: false });
  // 首开 → 配置流（非示例数据）
  await expect(page.getByRole('heading', { name: '连接你的智能中枢' })).toBeVisible();
  await expect(page.getByText('本地示例')).toHaveCount(0);
  // 错误令牌被拒
  await page.getByLabel('管理令牌').fill('wrong-token');
  await page.getByRole('button', { name: '认证并连接' }).click();
  await expect(page.getByRole('alert')).toContainText('访问令牌无效');
  // 正确令牌 → 偏好步
  await page.getByLabel('管理令牌').fill(TEST_TOKEN);
  await page.getByRole('button', { name: '认证并连接' }).click();
  await expect(page.getByRole('heading', { name: '一切就绪' })).toBeVisible();
  await page.getByRole('button', { name: '进入控制台' }).click();
  await expect(page.getByRole('heading', { name: '控制台', exact: true })).toBeVisible();
  // 令牌不落入 localStorage
  expect(
    await page.evaluate((t) => JSON.stringify(localStorage).includes(t), TEST_TOKEN),
  ).toBeFalsy();
  // 真实指标（夹具：记忆卡含 情节 128 · 偏好 36）
  await expect(page.locator('.metric').nth(2)).toContainText('情节 128 · 偏好 36');
  // 重载后会话保持（不再进首开流）
  await page.reload();
  await expect(page.getByRole('heading', { name: '控制台', exact: true })).toBeVisible();
  // 注销 → 回首开流
  await page.locator('a.nav-item[href="#settings"]').click();
  await page.getByRole('button', { name: '退出登录' }).click();
  await expect(page.getByRole('heading', { name: '连接你的智能中枢' })).toBeVisible();
});

test('overview shows service status and ops cards with trend chart', async ({ page }) => {
  await boot(page);
  await expect(page.getByRole('heading', { name: '控制台', exact: true })).toBeVisible();
  // 运行状态条
  await expect(page.locator('.status-strip')).toContainText('服务运行中');
  await expect(page.locator('.status-strip')).toContainText('production');
  // 运维卡：待处理（审批 2+任务 1=3）/设备 1/1/记忆队列 3/模型 deepseek-chat
  await expect(page.locator('.metric').nth(0)).toContainText('3');
  await expect(page.locator('.metric').nth(0)).toContainText('审批待决 2');
  await expect(page.locator('.metric').nth(1)).toContainText('1/1');
  await expect(page.locator('.metric').nth(2)).toContainText('3');
  await expect(page.locator('.metric').nth(2)).toContainText('情节 128 · 偏好 36');
  await expect(page.locator('.metric').nth(3)).toContainText('deepseek-chat');
  await page.getByLabel('趋势时间范围').selectOption('14');
  await expect(page.locator('.plot-hit-areas button')).toHaveCount(14);
  await page.locator('.event-row').first().click();
  await expect(page.getByRole('dialog')).toContainText('动态详情');
  await page.keyboard.press('Escape');
  await page.keyboard.press('Control+k');
  await page.getByRole('textbox', { name: '全局搜索' }).fill('设备');
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/#devices$/);
});

test('plugins page: focus card, toggle, health/test, config edit', async ({ page }) => {
  await boot(page);
  await page.goto('/#plugins');
  await expect(page.locator('.focus-card')).toHaveCount(2);
  // 聚焦 web_fetch 卡
  await page.locator('.focus-card').first().click();
  const card = page.getByRole('region', { name: '网页阅读' });
  await expect(card).toContainText('提取网页正文');
  await expect(card).toContainText('web.fetch');
  await expect(card).toContainText('健康：healthy');
  // 先测试（需启用态）
  await card.getByRole('button', { name: '测试插件' }).click();
  await expect(card).toContainText('测试通过 · 233 ms');
  // 启停开关（不触发折叠）：停用再启用
  await card.getByRole('switch', { name: '停用网页阅读' }).click();
  await expect(page.locator('.toast')).toContainText('已停用');
  await card.getByRole('switch', { name: '启用网页阅读' }).click();
  await expect(page.locator('.toast')).toContainText('已启用');
  // 配置编辑：改 timeout_s → 保存
  await card.locator('summary', { hasText: '插件配置' }).click();
  await card.getByLabel('配置值 timeout_s').fill('45');
  await card.getByRole('button', { name: '保存配置' }).click();
  await expect(card).toContainText('配置已保存');
});

test('memory: subjects browse, record detail, correct and delete', async ({ page }) => {
  await boot(page);
  await page.goto('/#memory');
  // 主体浏览：主人空间
  await expect(page.getByRole('heading', { name: '记忆', exact: true })).toBeVisible();
  await page.locator('.memory-subject-main', { hasText: '主人空间' }).click();
  await expect(page.locator('.memory-records .record-row')).toHaveCount(2);
  // 详情 → 纠正
  await page.locator('.memory-records .record-row').first().click();
  await expect(page.getByRole('dialog')).toContainText('偏好简洁');
  await page.getByRole('button', { name: '纠正' }).click();
  await page.getByLabel('新摘要').fill('偏好简洁、留白与 quiet 的表达方式');
  await page.getByRole('button', { name: '保存纠正' }).click();
  await expect(page.locator('.toast')).toContainText('已纠正');
  // 全局搜索
  await page.getByRole('tab', { name: '全局关键词搜索' }).click();
  await page.getByLabel('搜索记忆标题或内容...').fill('偏好');
  await expect(page.locator('.record-row').first()).toContainText('偏好简洁');
  await page.locator('.record-row').first().click();
  await page.getByRole('button', { name: '删除', exact: true }).click();
  await page.getByRole('button', { name: '确认删除' }).click();
  await expect(page.locator('.toast')).toContainText('已删除');
  const dl = page.waitForEvent('download');
  await page.getByRole('button', { name: '导出记忆' }).click();
  expect((await dl).suggestedFilename()).toBe('whale-memories.jsonl');
});

test('tasks list and pending approval decision', async ({ page }) => {
  await boot(page);
  await page.goto('/#tasks');
  await expect(page.locator('.record-row')).toHaveCount(2);
  await page.locator('.record-row').first().click();
  await expect(page.getByRole('dialog')).toContainText('整理下载文件夹');
  await page.getByRole('button', { name: '批准任务' }).click();
  await page.getByRole('button', { name: '确认操作' }).click();
  // 服务端口径：批准后任务保留并转为「已批准」
  await expect(page.locator('.record-row').first()).toContainText('已批准');
});

test('chat sends to service and shows reply', async ({ page }) => {
  await boot(page);
  await page.goto('/#chat');
  await page.getByRole('textbox', { name: '对话消息' }).fill('你好');
  await page.getByRole('button', { name: '发送消息' }).click();
  await expect(page.locator('.message.assistant').last()).toContainText(
    '这是来自测试服务的真实回复。',
  );
});

test('workflows list and run against the service', async ({ page }) => {
  await boot(page);
  await page.goto('/#workflows');
  await expect(page.locator('.workflow-card')).toHaveCount(1);
  await page.locator('.workflow-card').first().click();
  await page.getByPlaceholder('输入本次工作流的内容').fill('本周要点');
  await page.getByRole('button', { name: '运行工作流', exact: true }).click();
  await expect(page.locator('.flow-output')).toContainText('工作流执行完成');
});

test('devices page: topology, detail, rename, scopes and pause', async ({ page }) => {
  await boot(page);
  await page.goto('/#devices');
  await expect(page.getByRole('heading', { name: '设备与连接', exact: true })).toBeVisible();
  await expect(page.locator('.topology-core')).toContainText('Whale Core');
  await expect(page.locator('.topology-link')).toHaveCount(1);
  await expect(page.locator('.record-list .record-row')).toHaveCount(1);
  await expect(page.locator('.record-list')).toContainText('magic8-silent');
  // 详情 → 重命名
  await page.locator('.record-list .record-row').first().click();
  await expect(page.getByRole('dialog')).toContainText('magic8-silent');
  await page.getByRole('button', { name: '重命名' }).click();
  await page.getByLabel('设备名称').fill('magic8-silent（对账机）');
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await expect(page.getByRole('dialog')).toContainText('magic8-silent（对账机）');
  // 能力授权：关掉已有的 device.observe（3 → 2）
  await page.getByRole('tab', { name: '能力授权' }).click();
  await page.getByRole('switch', { name: /观察设备状态/ }).click();
  await page.getByRole('button', { name: '保存授权' }).click();
  await expect(page.getByRole('dialog')).toContainText('已选 2 / 4 项');
  // 暂停（双确认条）
  await page.getByRole('tab', { name: '概览' }).click();
  await page.getByRole('button', { name: '暂停设备' }).click();
  await page.getByRole('button', { name: '确认暂停' }).click();
  await expect(page.getByRole('dialog')).toContainText('已暂停');
  // 回执页签
  await page.getByRole('tab', { name: /回执/ }).click();
  await expect(page.locator('.receipt-row')).toHaveCount(2); // 表头 + 1 条
  await expect(page.locator('.receipt-table')).toContainText('device.shell');
});

test('approvals page: decide draft and task via service', async ({ page }) => {
  await boot(page);
  await page.goto('/#approvals');
  await expect(page.locator('.approval-card')).toHaveCount(2);
  await page.locator('.approval-card').first().getByRole('button', { name: '批准并投递' }).click();
  await expect(page.locator('.approval-card')).toHaveCount(1);
  await expect(page.locator('.decided-row').first()).toContainText('已批准');
  await page.locator('.approval-card').first().getByRole('button', { name: '拒绝' }).click();
  await expect(page.locator('.approval-card')).toHaveCount(0);
  await expect(page.getByText('当前没有待审批项')).toBeVisible();
});

test('events page: trace waterfall and live stream', async ({ page }) => {
  await boot(page);
  await page.goto('/#events');
  await expect(page.locator('.trace-item')).toHaveCount(1);
  await page.locator('.trace-row').first().click();
  await expect(page.locator('.span-row')).toHaveCount(2);
  await expect(page.locator('.trace-detail')).toContainText('回复已投递');
  await page.getByRole('tab', { name: '实时事件流' }).click();
  await expect(page.locator('.event-stream')).toContainText('device:transports');
});

test('settings: session info, diagnostics, classic link', async ({ page }) => {
  await boot(page);
  await page.goto('/#settings');
  await expect(page.locator('.connected-info')).toContainText('已连接到服务');
  await expect(page.locator('.connected-info')).toContainText('production');
  await expect(page.getByRole('link', { name: '打开原生控制台' })).toHaveAttribute(
    'href',
    '/classic/index.html',
  );
  await page.getByRole('button', { name: '运行诊断', exact: true }).click();
  await page.getByRole('button', { name: '开始诊断' }).click();
  await expect(page.locator('.diagnostic-list > div')).toHaveCount(2);
});

test('models page: five role cards, bind, effort, per-role test, add entry', async ({ page }) => {
  await boot(page);
  await page.goto('/#models');
  await expect(page.getByRole('heading', { name: '模型配置', exact: true })).toBeVisible();
  // 五张角色卡平级
  await expect(page.locator('.focus-card')).toHaveCount(5);
  // 绑定态：回复快=v4-pro-0813、意图=kimi-k3、视觉=qwen4-max；慢档/执行未绑定
  await expect(page.locator('.focus-card').nth(0)).toContainText('v4-pro-0813');
  await expect(page.locator('.focus-card').nth(1)).toContainText('未绑定');
  await expect(page.locator('.focus-card').nth(3)).toContainText('kimi-k3');
  // 聚焦回复慢档 → 绑定 kimi-k3
  await page.locator('.focus-card').nth(1).click();
  const slowCard = page.getByRole('region', { name: '回复模型（慢）' });
  await slowCard.getByLabel('回复模型（慢）绑定条目').selectOption('kimi-k3');
  await expect(page.locator('.toast')).toContainText('已绑定');
  await expect(page.locator('.focus-card').nth(1)).toContainText('kimi-k3');
  // 思考强度（条目级）：意图卡聚焦改档位
  await page.locator('.focus-card').nth(3).click();
  const intentCard = page.getByRole('region', { name: '意图模型' });
  await expect(intentCard.getByRole('radio', { name: 'High' })).toHaveAttribute('aria-checked', 'true');
  await intentCard.getByRole('radio', { name: 'Medium' }).click();
  await expect(page.locator('.toast')).toContainText('思考强度已保存');
  // 逐角色测试（已绑定意图）
  await intentCard.getByRole('button', { name: '测试该角色' }).click();
  await expect(intentCard).toContainText('连接成功');
  // 未绑定角色测试 → unbound 提示
  await page.locator('.focus-card').nth(4).click();
  const execCard = page.getByRole('region', { name: '任务执行模型' });
  await execCard.getByRole('button', { name: '测试该角色' }).click();
  await expect(execCard).toContainText('未绑定条目');
  // 添加模型条目
  await page.getByRole('button', { name: '添加模型条目' }).click();
  await page.getByLabel('条目名').fill('qwen4-turbo');
  await page.getByLabel('模型 id').fill('qwen4-turbo-0901');
  await page.getByRole('button', { name: '创建条目' }).click();
  await expect(page.locator('.toast')).toContainText('模型条目已保存');
});

test('personas page: cards, edit-save, create, import, playtest, effective', async ({ page }) => {
  await boot(page);
  await page.goto('/#personas');
  await expect(page.getByRole('heading', { name: '角色与人格', exact: true })).toBeVisible();
  // 两张角色卡平级
  await expect(page.locator('.focus-card')).toHaveCount(2);
  await expect(page.locator('.focus-card').nth(0)).toContainText('鲸鱼·默认');
  await expect(page.locator('.focus-card').nth(0)).toContainText('内置');
  // 聚焦编辑：改标题 → 保存 → PUT 体含改动
  await page.locator('.focus-card').nth(1).click();
  const card = page.getByRole('region', { name: '海底记录员' });
  await card.getByLabel('标题').fill('海底记录员·改');
  await card.getByRole('button', { name: '保存配置' }).click();
  await expect(page.locator('.toast')).toContainText('已保存');
  const write = state.personaWrites.at(-1)!;
  expect(write.method).toBe('PUT');
  expect(write.id).toBe('sea-scribe');
  expect((write.body as { title: string }).title).toBe('海底记录员·改');
  // 折叠展开不丢输入（在 重置 前先看草稿保持）
  // 新建流
  await page.getByRole('button', { name: '新建人物卡' }).click();
  const newCard = page.getByRole('region', { name: '新建人物卡' });
  await newCard.getByLabel('标题').fill('潮汐守望者');
  await newCard.getByLabel('角色名').fill('守望');
  await newCard.getByRole('button', { name: '创建人物卡' }).click();
  await expect(page.locator('.toast')).toContainText('已创建');
  expect(state.personas).toHaveLength(3);
  // 导入流
  await page.getByRole('button', { name: '导入卡片' }).click();
  await page.getByLabel('卡片内容').fill('{"name":"旅人"}');
  await page.getByRole('button', { name: '解析预览' }).click();
  await expect(page.getByRole('dialog')).toContainText('traveler');
  await page.getByRole('button', { name: '确认导入' }).click();
  await expect(page.locator('.toast')).toContainText('已导入');
  expect(state.personas).toHaveLength(4);
  // 试玩对照
  await page.locator('.focus-card').nth(0).click();
  const first = page.getByRole('region', { name: '鲸鱼·默认' });
  await first.getByRole('button', { name: '试玩' }).click();
  await page.getByLabel('测试输入').fill('你好');
  await page.getByRole('button', { name: '运行对照' }).click();
  await expect(page.getByRole('dialog')).toContainText('你好，我是鲸鱼。');
  await expect(page.getByRole('dialog')).toContainText('你好呀，旅人。');
  await page.keyboard.press('Escape');
  // 有效快照
  await first.getByRole('button', { name: '有效快照' }).click();
  await expect(page.getByRole('dialog')).toContainText('global_active');
  await expect(page.getByRole('dialog')).toContainText('你是鲸鱼，温和可靠。');
  await expect(page.getByRole('dialog')).toContainText('model_context');
});

test('tasks timeline, resume and workflows explore/settings/life', async ({ page }) => {
  await boot(page);
  // 任务时间线
  await page.goto('/#tasks');
  await page.locator('.record-row').first().click();
  await page.getByRole('button', { name: '查看执行时间线' }).click();
  await expect(page.getByRole('dialog')).toContainText('等待审批');
  await expect(page.getByRole('dialog')).toContainText('任务已规划');
  await expect(page.getByRole('dialog')).toContainText('tr-01');
  await expect(page.getByRole('dialog')).toContainText('not_dispatched');
  await page.keyboard.press('Escape');
  // 工作流页：探索 + 主动行为设置 + 兴趣台账
  await page.goto('/#workflows');
  await expect(page.locator('.workflow-card')).toHaveCount(1);
  await page.getByLabel('探索话题').fill('座头鲸迁徙');
  await page.getByRole('button', { name: '开始探索' }).click();
  await expect(page.locator('.flow-output')).toContainText('已创建探索任务');
  await expect(page.getByLabel('每日行动上限')).toHaveValue('12');
  await page.getByLabel('静默开始').fill('22:30');
  await page.getByRole('button', { name: '保存主动行为设置' }).click();
  await expect(page.locator('.toast')).toContainText('主动行为设置已保存');
  // 兴趣台账
  await expect(page.getByText('座头鲸迁徙').first()).toBeVisible();
  await expect(page.getByText('读了海洋声学文章')).toBeVisible();
  await page.getByLabel('新兴趣话题').fill('深海热泉');
  await page.getByRole('button', { name: '添加', exact: true }).click();
  await expect(page.locator('.toast')).toContainText('兴趣已添加');
  await page.getByRole('button', { name: '喜欢 座头鲸迁徙', exact: true }).click();
  await expect(page.locator('.toast')).toContainText('反馈已记录');
});

test('providers page: list, focus edit, test, models, create, delete', async ({ page }) => {
  await boot(page);
  await page.goto('/#providers');
  await expect(page.getByRole('heading', { name: '供应商管理', exact: true })).toBeVisible();
  await expect(page.locator('.focus-card')).toHaveCount(1);
  await expect(page.locator('.focus-card').first()).toContainText('DeepSeek');
  await expect(page.locator('.focus-card').first()).toContainText('已配置密钥');
  // 聚焦 → 编辑名称 + 保存
  await page.locator('.focus-card').first().click();
  const card = page.getByRole('region', { name: 'DeepSeek' });
  await card.getByLabel('供应商名称').fill('DeepSeek 官方');
  await card.getByRole('button', { name: '保存配置' }).click();
  await expect(page.locator('.toast')).toContainText('已保存');
  const write = state.providerWrites.at(-1)!;
  expect((write.body as { name: string }).name).toBe('DeepSeek 官方');
  // 连接测试 + 模型列表
  await card.getByRole('button', { name: '连接测试' }).click();
  await expect(card).toContainText('连接成功 · 188 ms');
  await card.getByRole('button', { name: '获取模型列表' }).click();
  await expect(card).toContainText('deepseek-v4-pro-0813');
  // 新增供应商
  await page.getByRole('button', { name: '新增供应商' }).click();
  await page.getByLabel('新供应商名称').fill('本地 vLLM');
  await page.getByLabel('新供应商 Base URL').fill('http://127.0.0.1:8000');
  await page.getByRole('button', { name: '创建', exact: true }).click();
  await expect(page.locator('.toast')).toContainText('已创建');
  await expect(page.locator('.focus-card')).toHaveCount(2);
  // 删除（双确认）
  await page.locator('.focus-card').nth(1).click();
  const second = page.getByRole('region', { name: '本地 vLLM' });
  await second.getByRole('button', { name: '删除' }).click();
  await page.getByRole('button', { name: '确认删除' }).click();
  await expect(page.locator('.toast')).toContainText('已删除');
  await expect(page.locator('.focus-card')).toHaveCount(1);
});

test('settings runtime config tab: budget, taboos, shell, services', async ({ page }) => {
  await boot(page);
  await page.goto('/#settings');
  await page.getByRole('button', { name: '运行配置', exact: true }).click();
  await expect(page.getByLabel('模型调用预算')).toHaveValue('200');
  await expect(page.getByText('今日已用 37 次')).toBeVisible();
  await expect(page.getByLabel('人格禁忌')).toHaveValue('不谈论政治');
  // 改预算 + 禁忌 → 保存 → 补丁校验
  await page.getByLabel('模型调用预算').fill('300');
  await page.getByLabel('人格禁忌').fill('不谈论政治\n23 点后提醒睡觉');
  await page.getByRole('switch', { name: '启用受控 Shell' }).click();
  await page.getByRole('button', { name: '保存运行配置' }).click();
  await expect(page.locator('.panel', { hasText: '运行配置' })).toContainText('已保存并生效');
  const patch = state.settingsPatches.at(-1) as {
    budget: { model_calls: number };
    persona_taboos: string[];
    services: Record<string, string>;
  };
  expect(patch.budget.model_calls).toBe(300);
  expect(patch.persona_taboos).toEqual(['不谈论政治', '23 点后提醒睡觉']);
  expect(patch.services.WHALE_SHELL_ENABLED).toBe('1');
});

test('events trace filters and logs time range', async ({ page }) => {
  await boot(page);
  // 事件页：Trace 类型/状态筛选
  await page.goto('/#events');
  await expect(page.locator('.trace-item')).toHaveCount(1);
  await page.getByLabel('Trace 类型').selectOption('task');
  await expect(page.locator('.trace-item')).toHaveCount(0);
  await page.getByLabel('Trace 类型').selectOption('');
  await expect(page.locator('.trace-item')).toHaveCount(1);
  // 日志页：时间范围
  await page.goto('/#logs');
  await expect(page.locator('.record-row').first()).toBeVisible();
  await page.getByLabel('时间范围').selectOption('today');
  await page.getByLabel('时间范围').selectOption('all');
  await expect(page.locator('.record-row').first()).toBeVisible();
});

test('no mock residue anywhere in the UI', async ({ page }) => {
  await boot(page);
  for (const p of [
    'overview',
    'chat',
    'memory',
    'tasks',
    'plugins',
    'workflows',
    'devices',
    'approvals',
    'events',
    'logs',
    'settings',
  ]) {
    await page.goto(`/#${p}`);
    await page.waitForTimeout(250);
    const body = await page.textContent('body');
    for (const banned of ['本地示例', '示例数据', '示例工作区', '本地助手', '演示模式']) {
      expect(body, `#${p} 不应出现「${banned}」`).not.toContain(banned);
    }
  }
});

test('mobile widths: bottom nav, sheets and no overflow', async ({ page }) => {
  await boot(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/');
  const nav = page.locator('.bottom-nav');
  await expect(nav).toBeVisible();
  await expect(nav.locator('a')).toHaveCount(5);
  await nav.locator('a[href="#devices"]').click();
  await expect(page).toHaveURL(/#devices$/);
  // 弹窗在窄屏转底部 Sheet
  await page.locator('.record-list .record-row').first().click();
  const modal = page.locator('dialog.modal');
  await expect(modal).toBeVisible();
  const box = (await modal.boundingBox())!;
  expect(box.width).toBe(390);
  await page.keyboard.press('Escape');
  // 各页无横向溢出
  for (const p of ['overview', 'devices', 'approvals', 'events', 'settings']) {
    await page.goto(`/#${p}`);
    await page.waitForTimeout(250);
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),
      p,
    ).toBeTruthy();
  }
  // 宽屏无底部导航
  await page.setViewportSize({ width: 1280, height: 900 });
  await expect(nav).not.toBeVisible();
});

test('authenticated upstream service works under its original CSP', async ({ page, context }) => {
  const url = process.env.WHALE_TEST_URL,
    token = process.env.WHALE_TEST_TOKEN;
  test.skip(!url || !token, 'A dedicated upstream test service is required.');
  const errors: string[] = [];
  page.on('pageerror', (e) => errors.push(e.message));
  // 首开：无会话 → 配置流
  await page.goto(`${url}/`);
  await expect(page.getByRole('heading', { name: '连接你的智能中枢' })).toBeVisible();
  // 错误令牌被拒
  await page.getByLabel('管理令牌').fill('invalid-test-token');
  await page.getByRole('button', { name: '认证并连接' }).click();
  await expect(page.getByRole('alert')).toContainText('访问令牌无效');
  // 正确令牌 → 配置完成 → 真实数据
  await page.getByLabel('管理令牌').fill(token!);
  await page.getByRole('button', { name: '认证并连接' }).click();
  await page.getByRole('button', { name: '进入控制台' }).click();
  await expect(page.locator('.header-status')).toContainText('服务在线');
  expect(
    await page.evaluate((t) => JSON.stringify(localStorage).includes(t), token!),
  ).toBeFalsy();
  // 重载后免手动连接（cookie 会话）
  await page.reload();
  await expect(page.getByRole('heading', { name: '控制台', exact: true })).toBeVisible();
  // classic 高级台
  const opened = context.waitForEvent('page');
  await page.locator('a.nav-item[href="#settings"]').click();
  await page.getByRole('link', { name: '打开原生控制台' }).click();
  const classic = await opened;
  await expect(classic).toHaveURL(/\/classic\/index.html/);
  await expect(classic.locator('#root')).not.toBeEmpty();
  await classic.close();
  // 插件启停回读（子页关闭后用 hash 直转，避免焦点切换期的行动性抖动）
  await page.goto(`${url}/#plugins`);
  const pluginCard = page.locator('.focus-card').filter({ hasText: 'web_fetch' });
  await pluginCard.click();
  const webReader = pluginCard.getByRole('switch');
  const wasEnabled = (await webReader.getAttribute('aria-checked')) === 'true';
  try {
    await webReader.click();
    await expect(webReader).toHaveAttribute('aria-checked', String(!wasEnabled));
  } finally {
    // 复原（用 Bearer 直接调 API）
    await context.request.post(`${url}/api/v1/plugins/web_fetch/${wasEnabled ? 'enable' : 'disable'}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
  }
  // 三新页真实服务渲染
  await page.locator('a.nav-item[href="#models"]').click();
  await expect(page.getByRole('heading', { name: '模型配置', exact: true })).toBeVisible();
  await expect(page.locator('.focus-card')).toHaveCount(3);
  await page.locator('.focus-card').first().click();
  await expect(
    page.getByRole('region', { name: '回复模型' }).getByLabel('供应商端点'),
  ).toBeVisible();
  await page.locator('a.nav-item[href="#personas"]').click();
  await expect(page.getByRole('heading', { name: '角色与人格', exact: true })).toBeVisible();
  await page.locator('a.nav-item[href="#devices"]').click();
  await expect(page.locator('.device-list-panel')).toBeVisible();
  await page.locator('a.nav-item[href="#approvals"]').click();
  await expect(page.getByRole('heading', { name: '审批队列', exact: true })).toBeVisible();
  await page.locator('a.nav-item[href="#events"]').click();
  await expect(page.locator('.trace-list')).toBeVisible();
  // 注销回配置流
  await page.locator('a.nav-item[href="#settings"]').click();
  await page.getByRole('button', { name: '退出登录' }).click();
  await expect(page.getByRole('heading', { name: '连接你的智能中枢' })).toBeVisible();
  expect(errors).toEqual([]);
});
