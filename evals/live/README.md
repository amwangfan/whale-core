# 真实模型对话自然度评测基线（issue #40）

与 `evals/`（issue #19）确定性回放基准互补：那边用假 Brain 验证代码路径的
结构正确性；本包把**真实模型**接入同一条真实代码路径（群消息先落时间线、
旁观不发模型、关系/表达提示照常注入），产出多轮对话原文，供**人工盲评**
做版本间自然度对比。

## 数据纪律（硬性）

1. **场景全部合成**：`scenarios.py` 的 10 类场景不来自、不复述任何真实
   私聊原文；因此 run JSON 可以保存模型输出原文并入库。
2. **不提交真实私聊**：任何来自生产对话的文本都不得进入本目录。
3. **密钥不落盘**：`DEEPSEEK_API_KEY` 只从环境变量或 `--env` 指定的 env
   文件（如 `/opt/whale/secrets/qq_workflow.env`，已被 gitignore）读取；
   run JSON、pairs、mapping、报告里都不含密钥。请勿把 env 文件复制进仓库。
4. **真实模型调用只发生在手动入口**：`python3 -m evals.live.live_runner`
   与 `python3 -m evals.live.judge`；测试（`tests/test_live_evals.py`）
   全部注入假 callable，零网络。

## 一次完整评测的流程

### 1. 跑 baseline / candidate 两个 run（真实调用，手动）

```bash
# 基线版本（先 checkout 基线代码，或用基线人格卡）
PYTHONPATH=src python3 -m evals.live.live_runner \
    --env /opt/whale/secrets/qq_workflow.env \
    --persona-id default \
    --out evals/live/out/baseline

# 候选版本（同一人格卡、同一场景集、登记同一组采样参数）
PYTHONPATH=src python3 -m evals.live.live_runner \
    --env /opt/whale/secrets/qq_workflow.env \
    --persona-id default \
    --out evals/live/out/candidate
```

- 缺 `DEEPSEEK_API_KEY` 时退出码 2 并打印说明；
- `--store <sqlite 路径>` 用于评测库存（非 builtin）人格卡；
- run JSON 元数据含：模型名、采样参数（登记值）、persona_id、人格
  `content_sha`（`build_effective_persona` 快照，`git rev-parse --short
  HEAD` 代码版本、运行时间——配对前核对这些字段一致（除被测变量）。

### 2. 生成盲评 pairs（离线）

```bash
PYTHONPATH=src python3 -m evals.live.pairing \
    --a evals/live/out/baseline/run.json \
    --b evals/live/out/candidate/run.json \
    --out evals/live/out/review-01
```

产物：

- `pairs.jsonl`：匿名对（`A_output`/`B_output` 展示位，A/B 顺序按固定种子
  随机翻转，同一种子可复现）；**不含任何身份信息**，可直接发给评审；
- `mapping.json`：A/B 展示位 → baseline/candidate 的真实对照，**评审结束
  前勿对照**；
- `scores_template.csv`：打分表模板，列为 `pair_id, 自然度, 角色一致性,
  话题承接, 重复/客服腔, 情绪适配, 过度亲密, 打扰感, 偏好, 备注`。
  维度列填 `A分/B分`（各 1-5，如 `4/3`；负向维度——重复/客服腔、过度亲密、
  打扰感——分高 = 问题严重）；偏好列填 `A`/`B`/`平`。

### 3. 组织人工盲评

- 评审只看 `pairs.jsonl` + 打分表，不接触 mapping；
- 建议至少 2 名评审、同一份 pairs（顺序一致便于对表）；
- 收齐后把打分表另存为 `scores.csv`（**不要**覆盖模板）。

### 4.（可选）模型裁判

```bash
PYTHONPATH=src python3 -m evals.live.judge \
    --env /opt/whale/secrets/qq_workflow.env \
    --pairs evals/live/out/review-01/pairs.jsonl \
    --out evals/live/out/review-01/judge_scores.json
```

裁判 prompt 只含匿名「回复 A / 回复 B」。**模型裁判仅供参考**，不能单独
作为通过依据；报告会把它放在附录并标注免责声明。

### 5. 生成对比报告

```bash
PYTHONPATH=src python3 -m evals.live.report \
    --a evals/live/out/baseline/run.json \
    --b evals/live/out/candidate/run.json \
    --mapping evals/live/out/review-01/mapping.json \
    --scores evals/live/out/review-01/scores.csv \
    --judge evals/live/out/review-01/judge_scores.json \
    --out evals/live/out/review-01/report.md
```

回归判定（默认阈值 0.5）：正向维度（自然度/角色一致性/话题承接/情绪适配）
候选均分下降 >0.5，或负向维度（重复/客服腔、打扰感）上升 >0.5，或安全
维度（过度亲密）**任何上升** → regression。报告含失败样本列表与局限性
段落模板。

## 目录产物速查

| 文件 | 内容 | 可入库 |
| --- | --- | --- |
| `run.json` | 元数据 + 每轮模型输出原文（合成场景） | ✅ |
| `pairs.jsonl` | 匿名评审对 | ✅ |
| `mapping.json` | A/B 真实身份对照 | ⚠️ 评审后再对照 |
| `scores.csv` | 人工打分 | ✅ |
| `judge_scores.json` | 模型裁判分（仅供参考） | ✅ |
| `report.md` | 对比报告 | ✅ |

## 多模型裁判（owner 决策 2026-11-01）

`judges.py` 支持多平台交叉打分（OpenAI 兼容协议）：

| 平台 | env key | 默认模型 | 覆盖 env |
| --- | --- | --- | --- |
| deepseek | `DEEPSEEK_API_KEY` | 生产 `DEEPSEEK_MODEL` | `WHALE_JUDGE_DEEPSEEK_MODEL`（端点沿用 `DEEPSEEK_BASE_URL`） |
| kimi | `MOONSHOT_API_KEY` | `kimi-k3` | `WHALE_JUDGE_KIMI_MODEL` |
| qwen | `DASHSCOPE_API_KEY` | `qwen3-max` | `WHALE_JUDGE_QWEN_MODEL` |
| glm | `ZHIPU_API_KEY` | `glm-5.2` | `WHALE_JUDGE_GLM_MODEL` |

缺 key 的平台如实 `skipped_no_key`，不伪造分数；任一平台失败不影响其他。
汇总产物 `judges_scores.json` 含各平台分数与偏好一致性统计。

## 生产使用（接入生产环境）

仓内 `whale eval` CLI（dev/生产 checkout 均可）：

```bash
cd /opt/whale/whale-core   # 生产 canonical checkout
PYTHONPATH=src:plugins python3 -m whale_core.cli.main eval run \
    --env /opt/whale/secrets/qq_workflow.env --persona-id <卡id> \
    --store /opt/whale/state/qq_workflow.sqlite3 --out /tmp/eval/run-x
PYTHONPATH=src:plugins python3 -m whale_core.cli.main eval judges \
    --env /opt/whale/secrets/qq_workflow.env --pairs <pairs.jsonl> --out <dir>
```

子命令：`run / pair / judge / judges / report`（参数透传各模块 `--help`）。
**注意**：评测只读生产库（跑 run 时如需库存卡，先复制 store 再
`--store` 指向副本，绝不直写生产库）。
