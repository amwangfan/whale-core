#!/usr/bin/env python3
"""冒烟：模拟 owner 私聊「做一张简单结果图发我」→ do_request → ResultDelivery。

用法（N100）：
  PYTHONPATH=src:plugins python3 scripts/smoke_do_request.py \
    --secrets /opt/whale/secrets/qq_workflow.env
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _load_env(path: Path) -> dict:
    out = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--secrets", required=True)
    ap.add_argument("--text", default="做一张简单结果图发我")
    args = ap.parse_args(argv)
    env = _load_env(Path(args.secrets))
    for k, v in env.items():
        os.environ.setdefault(k, v)

    sys.path[:0] = ["src", "plugins"]
    from weq_channel.full_mode_window import FullModeWindow, WindowedSendPort
    from weq_channel.weq_send import WeQWebSendPort
    from whale_core.life.do_request import try_owner_do

    base = env.get("WEQ_WEB_URL") or "http://127.0.0.1:7690"
    token = env["WEQ_WEB_TOKEN"]
    owner_uid = env["OWNER_UID"]
    self_uin = env.get("QQ_SELF_UIN") or None

    port = WeQWebSendPort(base, token, target_uid=owner_uid,
                          expected_account_uin=self_uin)
    if os.environ.get("QQ_FULLMODE_ONDEMAND", "").strip() == "1":
        window = FullModeWindow(
            base, token,
            state_file=os.environ.get("QQ_FULLMODE_STATE",
                                      "/tmp/whale-fullmode-send.ts"))
        port = WindowedSendPort(port, window)

    conversation = f"private:{owner_uid}"
    out = try_owner_do(
        args.text,
        conversation=conversation,
        send_port=port,
        owner_uid=owner_uid,
    )
    print({"text": args.text, "result": out})
    if not out or not out.get("handled"):
        return 2
    delivery = out.get("delivery") or {}
    return 0 if delivery.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
