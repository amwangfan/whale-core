"""issue #47：本地运行时归属管理（LocalRuntimeManager）离线单测。

所有 systemctl / git / /proc / 网络探测均注入 fake，不触真实系统。
"""

from __future__ import annotations

import pytest

from whale_core.cli.main import build_parser
from whale_core.ops.local_runtime import (
    LocalRuntimeManager,
    RuntimeError_,
    ServiceSpec,
    render_json,
    render_text,
)

CHECKOUT = "/opt/whale/whale-core"
HEAD_A = "a" * 40
HEAD_B = "b" * 40

API = ServiceSpec(
    name="whale-api", unit="whale-api.service", ports=(8180,),
    cmdline_marker="whale-api", health_url="http://127.0.0.1:8180/api/v1/health",
)
QQ = ServiceSpec(
    name="qq-bot", unit="qq-bot.service", ports=(18797,),
    cmdline_marker="whale_core.cli.main", health_tcp=("127.0.0.1", 18797),
)


def make_manager(
    *,
    unit_state=None,           # {unit: (active, pid)}
    cwds=None,                 # {pid: cwd}
    cmdlines=None,             # {pid: cmdline}
    listeners=None,            # [(addr, port, pid)]
    heads=None,                # {path: head}
    http=None,                 # {url: (code, body)}
    tcp=None,                  # {(host, port): bool}
):
    unit_state = unit_state or {}
    cwds = cwds or {}
    cmdlines = cmdlines or {}
    listeners = listeners or []
    heads = heads or {}
    http = http or {}
    tcp = tcp or {}
    calls: list[list[str]] = []

    def runner(cmd):
        calls.append(list(cmd))
        if cmd[0] == "systemctl" and cmd[1] == "show":
            active, pid = unit_state.get(cmd[2], (False, None))
            return 0, (
                f"ActiveState={'active' if active else 'inactive'}\n"
                f"MainPID={pid or 0}\n"
            ), ""
        if cmd[0] == "systemctl" and cmd[1] in ("start", "stop"):
            return 0, "", ""
        if cmd[0] == "git":
            head = heads.get(cmd[2])
            return (0, f"{head}\n", "") if head else (128, "", "not a repo")
        if cmd[0] == "ps":
            pid = int(cmd[-1])
            line = cmdlines.get(pid)
            return (0, f"{line}\n", "") if line else (1, "", "")
        return 1, "", "unexpected"

    mgr = LocalRuntimeManager(
        CHECKOUT, [API, QQ],
        runner=runner,
        cwd_of_pid=lambda pid: cwds.get(pid),
        port_listeners=lambda: list(listeners),
        http_probe=lambda url, timeout: http.get(url, (0, "")),
        tcp_probe=lambda host, port, timeout: tcp.get((host, port), False),
        now=lambda: 1000.0,
    )
    return mgr, calls


def healthy_world():
    """两个服务都 active、cwd 在 canonical、HEAD 一致、端口归属正确。"""
    return dict(
        unit_state={"whale-api.service": (True, 100), "qq-bot.service": (True, 200)},
        cwds={100: f"{CHECKOUT}/src", 200: CHECKOUT, 999: "/"},
        cmdlines={
            100: "python3 /opt/whale/scripts/whale-api.py",
            200: "python3 -m whale_core.cli.main qq run",
        },
        listeners=[
            ("127.0.0.1", 8180, 100),
            ("100.114.93.90", 8180, 999),   # tailscale 代理：不同地址，不算冲突
            ("127.0.0.1", 18797, 200),
        ],
        heads={CHECKOUT: HEAD_A, f"{CHECKOUT}/src": HEAD_A},
        http={"http://127.0.0.1:8180/api/v1/health": (200, "ok")},
        tcp={("127.0.0.1", 18797): True},
    )


class TestStatus:
    def test_healthy(self):
        mgr, _ = make_manager(**healthy_world())
        report = mgr.status()
        assert report.ok
        assert report.canonical_head == HEAD_A
        api = report.services[0]
        assert api.active and api.main_pid == 100
        assert api.cwd_in_canonical and api.head_matches_canonical
        assert api.health == "ok"

    def test_foreign_cwd_flagged(self):
        world = healthy_world()
        world["cwds"][100] = "/opt/whale/whale-core-dev"
        world["heads"]["/opt/whale/whale-core-dev"] = HEAD_A
        mgr, _ = make_manager(**world)
        report = mgr.status()
        api = report.services[0]
        assert not report.ok
        assert not api.cwd_in_canonical
        assert any("不在 canonical" in p for p in api.problems)

    def test_head_drift_flagged(self):
        world = healthy_world()
        world["heads"][f"{CHECKOUT}/src"] = HEAD_B
        mgr, _ = make_manager(**world)
        api = mgr.status().services[0]
        assert api.head_matches_canonical is False
        assert any("不一致" in p for p in api.problems)

    def test_foreign_port_owner_flagged(self):
        world = healthy_world()
        # 同地址 127.0.0.1:8180 上多出一个外来监听者 → 冲突
        world["listeners"].append(("127.0.0.1", 8180, 555))
        world["cwds"][555] = "/tmp/somewhere-else"
        mgr, _ = make_manager(**world)
        api = mgr.status().services[0]
        assert any("外来进程" in p and "555" in p for p in api.problems)

    def test_other_addr_same_port_not_conflict(self):
        # tailscale 在 tailnet 地址上代理同端口 → 健康（healthy_world 已含）
        mgr, _ = make_manager(**healthy_world())
        report = mgr.status()
        assert report.ok
        api = report.services[0]
        assert 999 in api.ports[0].pids or not api.problems

    def test_inactive_service_clean(self):
        mgr, _ = make_manager(heads={CHECKOUT: HEAD_A})
        report = mgr.status()
        assert report.ok
        assert all(not s.active for s in report.services)

    def test_health_fail_flagged_when_active(self):
        world = healthy_world()
        world["http"] = {}
        mgr, _ = make_manager(**world)
        api = mgr.status().services[0]
        assert api.health == "fail"
        assert any("健康检查失败" in p for p in api.problems)


class TestStart:
    def test_start_inactive(self):
        mgr, calls = make_manager(heads={CHECKOUT: HEAD_A})
        mgr.start("whale-api")
        assert ["systemctl", "start", "whale-api.service"] in calls

    def test_start_idempotent_when_active(self):
        mgr, calls = make_manager(**healthy_world())
        st = mgr.start("whale-api")
        assert st.active
        assert not any(c[:2] == ["systemctl", "start"] for c in calls)

    def test_start_refused_on_foreign_port(self):
        mgr, calls = make_manager(
            heads={CHECKOUT: HEAD_A},
            listeners=[("127.0.0.1", 8180, 555)],
            cwds={555: "/home/other/repo"},
        )
        with pytest.raises(RuntimeError_, match="外来进程"):
            mgr.start("whale-api")
        assert not any(c[:2] == ["systemctl", "start"] for c in calls)

    def test_start_ignores_other_addr_listener(self):
        # 其他地址上的同端口监听（tailscale 代理）不阻止启动
        mgr, calls = make_manager(
            heads={CHECKOUT: HEAD_A},
            listeners=[("100.114.93.90", 8180, 999)],
            cwds={999: "/"},
        )
        mgr.start("whale-api")
        assert ["systemctl", "start", "whale-api.service"] in calls

    def test_start_allows_port_owned_in_canonical(self):
        # 端口被 canonical 内的进程占用（如手动直跑）→ 不拒绝，交给 systemd 判定
        mgr, calls = make_manager(
            heads={CHECKOUT: HEAD_A},
            listeners=[("127.0.0.1", 8180, 555)],
            cwds={555: f"{CHECKOUT}/src"},
        )
        mgr.start("whale-api")
        assert ["systemctl", "start", "whale-api.service"] in calls


class TestStop:
    def test_stop_happy_path(self):
        mgr, calls = make_manager(**healthy_world())
        mgr.stop("qq-bot")
        assert ["systemctl", "stop", "qq-bot.service"] in calls

    def test_stop_refused_foreign_cwd(self):
        world = healthy_world()
        world["cwds"][200] = "/opt/whale/whale-core-dev"
        mgr, calls = make_manager(**world)
        with pytest.raises(RuntimeError_, match="不在"):
            mgr.stop("qq-bot")
        assert not any(c[:2] == ["systemctl", "stop"] for c in calls)

    def test_stop_refused_marker_mismatch(self):
        world = healthy_world()
        world["cmdlines"][200] = "python3 /tmp/evil.py"
        mgr, calls = make_manager(**world)
        with pytest.raises(RuntimeError_, match="登记标记"):
            mgr.stop("qq-bot")
        assert not any(c[:2] == ["systemctl", "stop"] for c in calls)

    def test_stop_inactive_is_noop(self):
        mgr, calls = make_manager(heads={CHECKOUT: HEAD_A})
        mgr.stop("qq-bot")
        assert not any(c[:2] == ["systemctl", "stop"] for c in calls)


class TestRenderAndCli:
    def test_render_text(self):
        mgr, _ = make_manager(**healthy_world())
        text = render_text(mgr.status())
        assert "canonical checkout" in text
        assert "[whale-api] active pid=100" in text
        assert "port 8180" in text

    def test_render_json_shape(self):
        mgr, _ = make_manager(**healthy_world())
        import json
        data = json.loads(render_json(mgr.status()))
        assert data["canonical_head"] == HEAD_A
        assert data["services"][0]["ports"][0]["port"] == 8180

    def test_cli_parser(self):
        args = build_parser().parse_args(["runtime", "status", "--json"])
        assert args.runtime_command == "status"
        args2 = build_parser().parse_args(["runtime", "stop", "whale-api"])
        assert args2.services == ["whale-api"]

    def test_unknown_service(self):
        mgr, _ = make_manager()
        with pytest.raises(RuntimeError_, match="未知服务"):
            mgr.start("nope")
