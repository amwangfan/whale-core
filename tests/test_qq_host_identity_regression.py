"""Host identity assembly regression; execute only the AST slice, never the runner."""
from __future__ import annotations

import ast
import builtins
import glob
import io
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


RUNNER = Path(__file__).resolve().parents[1] / "examples" / "qq_live_workflow.py"


@pytest.mark.parametrize("configured_uid", ["", "configured-uid"])
def test_discovered_self_uid_reaches_both_companions(monkeypatch, configured_uid):
    tree = ast.parse(RUNNER.read_text(encoding="utf-8"))
    # Locate the enclosing assembly function without importing/executing its entrypoint.
    host = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                and any(isinstance(s, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "target_resolver"
                                for t in s.targets) for s in n.body))
    start = next(i for i, n in enumerate(host.body) if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "self_uid" for t in n.targets))
    end = next(i for i, n in enumerate(host.body) if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "target_resolver" for t in n.targets))
    monkeypatch.setenv("QQ_SELF_UID", configured_uid)
    monkeypatch.setattr(glob, "glob", lambda pattern: ["/synthetic/account.json"])
    monkeypatch.setattr(builtins, "open", lambda *a, **k: io.StringIO(
        '{"uin":"synthetic-bot","uid":"discovered-uid"}'))
    companion = SimpleNamespace(timeline=object(), planning_hint="")
    heavy = SimpleNamespace()
    workflow = SimpleNamespace(companion=companion,
                               orchestrator=SimpleNamespace(heavy_companion=heavy))
    namespace = {"os": os, "workflow": workflow, "self_uin": "synthetic-bot",
                 "tools": SimpleNamespace(for_capability=lambda cap: []),
                 "reaction_store": SimpleNamespace(list=lambda: []),
                 "ReplyTargetResolver": lambda **kw: SimpleNamespace(**kw)}
    exec(compile(ast.Module(body=host.body[start:end + 1], type_ignores=[]),
                 str(RUNNER), "exec"), namespace)
    expected = configured_uid or "discovered-uid"
    assert namespace["target_resolver"].self_uid == expected
    assert companion.self_uid == expected
    assert heavy.self_uid == expected
    hint = companion.planning_hint()
    for excluded in ("答应下来即可", "别再说自己读不了图", "不是瞎猜", "不互损怼人", "别用破折号"):
        assert excluded not in hint
