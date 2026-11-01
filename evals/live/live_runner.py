"""真实模型驱动器（issue #40）：真实模型走 issue #19 的同一条真实代码路径。

纪律：

- 群消息先落 ``GroupTimelineStore``、旁观消息**不发模型**、回复目标裁决 /
  关系视图 / 表达策略全部走真实实现——直接复用 ``evals.runner._World``
  的世界搭建与 ``step`` 流程，仅把 ``FakeBrain`` 换成注入的 ``model_reply``；
- 人格经 ``build_effective_persona``（issue #36 唯一渲染器）取快照，元数据
  记录 ``content_sha``；同时把卡绑定进每个会话，让 companion 的 G4 链路
  （关系/表达提示追加）与生产一致；
- 每条模型输出原文随 run JSON 保存（场景全部合成，无私聊原文）；
- **密钥纪律**：``build_deepseek_reply`` 只从给定的 env 映射读
  ``DEEPSEEK_API_KEY`` / ``DEEPSEEK_MODEL`` / ``DEEPSEEK_BASE_URL``，
  绝不把密钥写进 run JSON、日志或仓库；
- **真实模型调用只允许发生在手动入口**（``python3 -m evals.live.live_runner``），
  测试一律注入假 callable（见 tests/test_live_evals.py）。

手动运行（真实调用模型）::

    PYTHONPATH=src python3 -m evals.live.live_runner \
        --env /opt/whale/secrets/qq_workflow.env \
        --persona-id default --out evals/live/out/baseline
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Mapping, Optional, Sequence

from whale_core.life.context_resolver import derive_kind
from whale_core.ui.effective_persona import build_effective_persona
from whale_core.ui.persona_cards import CARD_KEY_PREFIX, CUSTOM_TABOOS_KEY

from evals.runner import _World
from evals.scenarios import CHANNEL

#: 注入模型的 callable 形态：与 DeepSeekCompanion.reply 同构。
ModelReply = Callable[..., str]

#: run JSON 格式版本（结构变更时递增）。
RUN_FORMAT = "whale-live-run/1"

#: run JSON 文件名（每个 out_dir 一份；多次运行由调用方换目录区分）。
RUN_FILENAME = "run.json"


class LiveEvalConfigError(RuntimeError):
    """真实模型装配失败（缺 key 等配置问题）——CLI 以退出码 2 报告。"""


class LiveRunError(RuntimeError):
    """评测运行期失败（模型返回空 / 非文本等）。"""


# ---------------------------------------------------------------------------
# 模型适配：把注入的 callable 包成 companion 期望的 Brain 形态
# ---------------------------------------------------------------------------


class _ModelReplyAdapter:
    """``reply(text, **kwargs)`` 录制调用并转发给注入的 ``model_reply``。

    与 ``evals.runner.FakeBrain`` 的唯一区别：返回值来自外部 callable
    （真实模型或测试假件），不保证确定性。
    """

    def __init__(self, model_reply: ModelReply) -> None:
        self._model_reply = model_reply
        self.calls: list = []

    def reply(self, text: str, *, persona: str = "", memories=(), history=(),
              images=()) -> str:
        from evals.runner import BrainCall

        self.calls.append(BrainCall(
            text=text, persona=persona,
            memories=tuple(str(m) for m in memories),
            history=tuple(dict(h) for h in history),
        ))
        out = self._model_reply(
            text, persona=persona,
            memories=tuple(str(m) for m in memories),
            history=tuple(dict(h) for h in history),
        )
        if not isinstance(out, str) or not out.strip():
            raise LiveRunError(
                f"model_reply 返回了空/非文本结果（输入前 40 字：{text[:40]!r}）")
        return out


class _LiveWorld(_World):
    """issue #19 的回放世界 + 真实模型 Brain + 会话级人格绑定（issue #36）。"""

    def __init__(self, scenario, model_reply: ModelReply, *,
                 persona_store, persona_id: str) -> None:
        super().__init__(scenario)
        adapter = _ModelReplyAdapter(model_reply)
        self.brain = adapter
        self.companion.model = adapter
        # 把被测人格卡 / 自定义禁忌复制进本场景世界（builtin 卡经 library
        # 解析无需复制），并把卡绑定到本场景出现的每个会话——companion 的
        # S3 链路与生产同口径取卡渲染。
        stored = persona_store.get(f"{CARD_KEY_PREFIX}{persona_id}")
        if isinstance(stored, dict):
            self.store.put(f"{CARD_KEY_PREFIX}{persona_id}", stored)
        taboos = persona_store.get(CUSTOM_TABOOS_KEY)
        if isinstance(taboos, list):
            self.store.put(CUSTOM_TABOOS_KEY, taboos)
        convs = {msg.conversation_id for msg in scenario.messages}
        for cid in sorted(convs):
            conv_key = f"{CHANNEL}:{derive_kind(CHANNEL, cid).value}:{cid}"
            self.bindings.bind_persona(conv_key, persona_id,
                                       updated_by="evals.live")


# ---------------------------------------------------------------------------
# run JSON
# ---------------------------------------------------------------------------


def _code_revision() -> str:
    """当前代码的 ``git rev-parse --short HEAD``；非 git 环境回退 "unknown"。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=str(Path(__file__).resolve().parents[2]),
        )
        rev = (out.stdout or "").strip()
        return rev if out.returncode == 0 and rev else "unknown"
    except Exception:  # 无 git / 超时等一律不阻塞评测
        return "unknown"


def _input_digest(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def _persona_sha(persona_text: str) -> str:
    return hashlib.sha256(persona_text.encode("utf-8")).hexdigest()[:12]


def run_live(model_reply: ModelReply, scenarios: Sequence, *,
             persona_store, persona_id: str, model_name: str,
             sampling: Mapping, out_dir,
             revision: Optional[str] = None,
             now: Optional[str] = None) -> dict:
    """逐场景驱动真实模型，产出 run JSON（写入 ``out_dir/run.json`` 并返回）。

    参数：

    - ``model_reply``：注入的 callable（真实模型或测试假件），形态
      ``reply(text, *, persona=, memories=, history=) -> str``；
    - ``persona_store``：含被测人格卡的 MemoryStore（builtin 卡可给空仓）；
    - ``sampling``：采样参数映射（仅作元数据登记，便于配对复现时核对）；
    - ``revision`` / ``now``：测试注入用；默认取 git HEAD 与当前 UTC 时间。
    """
    snapshot = build_effective_persona(persona_store, persona_id)
    run: dict = {
        "format": RUN_FORMAT,
        "meta": {
            "model_name": model_name,
            "sampling": dict(sampling),
            "persona_id": snapshot.persona_id,
            "persona_content_sha": snapshot.content_sha,
            "persona_source": snapshot.source,
            "code_revision": revision if revision is not None else _code_revision(),
            "generated_at": (now if now is not None else
                             datetime.now(timezone.utc).isoformat()),
        },
        "scenarios": [],
    }
    model_calls = 0
    for scenario in scenarios:
        world = _LiveWorld(scenario, model_reply,
                           persona_store=persona_store, persona_id=persona_id)
        world.seed()
        turns: list[dict] = []
        for index, msg in enumerate(scenario.messages):
            record = world.step(msg)
            model_calls += 1 if record.reply_action == "reply" else 0
            turns.append({
                "turn_index": index,
                "message_id": record.message_id,
                "sender_id": record.sender_id,
                "conversation_id": record.conversation_id,
                "is_group": record.is_group,
                "action": record.reply_action,
                "input_text": record.text,
                "input_digest": _input_digest(record.text),
                # 旁观轮无模型输出（纪律：不发模型）；回复轮为模型原文。
                "output_text": record.reply_text,
                # 该轮实际注入人格的 sha（审计用；与 meta.content_sha 的
                # 关系见 README：companion 会在快照后追加关系/表达提示）。
                "persona_injected_sha": (
                    _persona_sha(record.persona_injected)
                    if record.persona_injected else ""),
            })
        run["scenarios"].append({
            "scenario_id": scenario.scenario_id,
            "title": scenario.title,
            "category": scenario.category,
            "focus": list(scenario.focus),
            "turns": turns,
        })
    run["meta"]["scenario_count"] = len(run["scenarios"])
    run["meta"]["model_calls"] = model_calls
    write_run(run, out_dir)
    return run


def write_run(run: Mapping, out_dir) -> Path:
    """把 run dict 写入 ``out_dir/run.json``（UTF-8，不转义中文）。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / RUN_FILENAME
    path.write_text(json.dumps(run, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    return path


def load_run(path) -> dict:
    """读取 run JSON 并做最小结构校验。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("format") != RUN_FORMAT:
        raise ValueError(f"不是 {RUN_FORMAT} 格式的 run JSON：{path}")
    if not isinstance(data.get("meta"), dict) or not isinstance(
            data.get("scenarios"), list):
        raise ValueError(f"run JSON 缺少 meta/scenarios：{path}")
    return data


# ---------------------------------------------------------------------------
# 真实模型装配（仅手动入口使用；密钥不落盘）
# ---------------------------------------------------------------------------


def load_env_file(path) -> dict:
    """解析 ``KEY=VALUE`` env 文件（# 开头注释、空行跳过；值可带单/双引号）。"""
    env: dict = {}
    for lineno, raw in enumerate(
            Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue  # 宽容跳过非赋值行（如多行注释残片）
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            env[key] = value
    return env


def build_deepseek_reply(env: Mapping[str, str]) -> ModelReply:
    """从 env 映射装配真实 DeepSeek callable。

    缺 ``DEEPSEEK_API_KEY`` 时抛 ``LiveEvalConfigError``（CLI 以退出码 2
    报告并说明）；返回的 callable 与 ``DeepSeekCompanion.reply`` 同构。
    密钥只保存在内存中的 companion 实例里，本模块绝不输出。
    """
    from whale_core.core.deepseek import DeepSeekCompanion

    api_key = (env.get("DEEPSEEK_API_KEY") or "").strip()
    base_url = (env.get("DEEPSEEK_BASE_URL") or "").strip() or None
    if not api_key and base_url is None:
        raise LiveEvalConfigError(
            "缺少 DEEPSEEK_API_KEY：请通过 --env 指定含该变量的 env 文件，"
            "或先 export DEEPSEEK_API_KEY=...（密钥绝不写入仓库与任何输出）")
    try:
        companion = DeepSeekCompanion(
            api_key=api_key,
            base_url=base_url,
            model=(env.get("DEEPSEEK_MODEL") or "").strip() or None,
        )
    except ValueError as exc:
        raise LiveEvalConfigError(f"模型端点配置无效：{exc}") from exc

    def reply(text: str, *, persona: str = "", memories=(), history=()) -> str:
        return companion.reply(text, persona=persona, memories=memories,
                               history=history)

    reply.model_name = companion.model  # type: ignore[attr-defined]
    return reply


def _build_persona_store(store_path: Optional[str]):
    """被测人格卡的存储：给了 sqlite 路径用生产库，否则空仓（builtin 卡）。"""
    if store_path:
        from whale_core.memory.sqlite_store import SqliteMemoryStore

        return SqliteMemoryStore(store_path)
    from whale_core.memory.store import InMemoryStore

    return InMemoryStore()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """手动入口（真实调用模型）。配置缺失退出码 2；运行期失败退出码 1。"""
    import argparse
    import sys

    from evals.live.scenarios import LIVE_SCENARIOS, LIVE_SCENARIOS_BY_ID

    parser = argparse.ArgumentParser(
        prog="python3 -m evals.live.live_runner",
        description="真实模型对话自然度评测（issue #40）——手动运行，"
                    "真实调用模型；密钥只从 env 读取，绝不落盘。")
    parser.add_argument("--env", default=None,
                        help="含 DEEPSEEK_API_KEY 的 env 文件路径"
                             "（缺省只读当前进程环境变量）")
    parser.add_argument("--persona-id", default="default",
                        help="被测人格卡 id（默认 builtin default）")
    parser.add_argument("--store", default=None,
                        help="生产 sqlite 库路径（被测卡为库存卡时必填；"
                             "builtin 卡可省略）")
    parser.add_argument("--out", required=True, help="run JSON 输出目录")
    parser.add_argument("--model", default=None,
                        help="覆盖 DEEPSEEK_MODEL（仅登记与传名，不碰密钥）")
    parser.add_argument("--temperature", type=float, default=None,
                        help="采样温度（元数据登记，便于配对复现核对）")
    parser.add_argument("--top-p", type=float, default=None,
                        help="top-p（元数据登记）")
    parser.add_argument("--scenario", action="append", default=None,
                        help="只跑指定场景 id（可重复；缺省跑全部）")
    args = parser.parse_args(argv)

    env = dict(os.environ)
    if args.env:
        try:
            env.update(load_env_file(args.env))
        except OSError as exc:
            print(f"读取 env 文件失败：{exc}", file=sys.stderr)
            return 2
    if args.model:
        env["DEEPSEEK_MODEL"] = args.model
    try:
        model_reply = build_deepseek_reply(env)
    except LiveEvalConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    if args.scenario:
        unknown = [sid for sid in args.scenario
                   if sid not in LIVE_SCENARIOS_BY_ID]
        if unknown:
            print(f"未知场景 id：{', '.join(unknown)}", file=sys.stderr)
            return 2
        scenarios = tuple(LIVE_SCENARIOS_BY_ID[sid] for sid in args.scenario)
    else:
        scenarios = LIVE_SCENARIOS

    sampling = {}
    if args.temperature is not None:
        sampling["temperature"] = args.temperature
    if args.top_p is not None:
        sampling["top_p"] = args.top_p

    persona_store = _build_persona_store(args.store)
    model_name = args.model or env.get("DEEPSEEK_MODEL") or "deepseek-chat"
    try:
        run = run_live(model_reply, scenarios,
                       persona_store=persona_store,
                       persona_id=args.persona_id,
                       model_name=model_name,
                       sampling=sampling,
                       out_dir=args.out)
    except LiveRunError as exc:
        print(f"运行失败：{exc}", file=sys.stderr)
        return 1
    out_path = Path(args.out) / RUN_FILENAME
    print(f"run JSON 已写入：{out_path}")
    print(f"场景 {run['meta']['scenario_count']} 个，"
          f"模型调用 {run['meta']['model_calls']} 次，"
          f"人格 content_sha={run['meta']['persona_content_sha']}")
    return 0


if __name__ == "__main__":  # pragma: no cover —— 手动运行，真实调用模型
    raise SystemExit(main())
