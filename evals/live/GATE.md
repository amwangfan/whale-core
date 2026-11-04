# issue #53 对话质量回归门禁

## 复用什么

仓内已有 `evals/live/`（场景 L01–L14、live_runner、pairing、judges、report、
`whale eval` CLI）。公开常见做法：固定脚本化场景 + 人工/模型裁判，门禁看
**相对回归**而非绝对分。本仓沿用该思路，不另起评测平台。

## 门禁分层

| 层 | 命令 | 何时 | 是否打真实模型 |
|----|------|------|----------------|
| 离线结构 | `pytest tests/test_live_evals.py tests/test_persona_evals.py -q` | 每次合入相关改动 | 否（假 callable） |
| 抽检（companion） | `python3 -m evals.live.live_runner --preset companion --env … --out …` | 人格/companion 改动后手动 | 是 |
| 抽检（glasses） | `… --preset glasses …` | 眼镜 `/v1/chat` 路径改动后手动 | 是 |
| 全量盲评 | 全场景 run → pair → 人工 scores → report | 发版前 / 人格大改 | 是 |

## 通过标准（人工盲评）

见 `README.md`：正向维度候选均分下降 >0.5，或负向上升 >0.5，或「过度亲密」
任何上升 → regression。模型裁判仅附录，不能单独过门。

## 自研什么（本回合）

- `--preset companion|glasses` 抽检子集
- 本 GATE 说明（CI 仍为 workflow_dispatch；真实模型不进自动 CI）
