"""whale CLI：setup / doctor / run / plugins / memory（roadmap §9 入口）。

设计约定（与仓内安全约定一致）：

- ``config.json`` 只存非敏感信息与 **env 引用名**（如 api_key_env），
  绝不写密钥本体；doctor 只报告「已配置/未配置」，不回显 key；
- 配置目录默认 ``~/.whale/``，内含 ``config.json`` / ``memory.sqlite3`` /
  ``manifests/``（插件清单目录，显式加载、不做自动发现）；
- ``main(argv)`` 返回退出码，库函数绝不 ``sys.exit``（argparse 自身的
  SystemExit 除外）；``python3 -m whale_core.cli.main`` 可直接运行；
- 没有 demo/mock 档（owner 决策）：run 连接配置档对应的真实模型端点。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

from whale_core.core.companion import WhaleCompanion
from whale_core.core.runtime import ConversationOrchestrator
from whale_core.memory.export import export_jsonl, import_jsonl
from whale_core.memory.sqlite_store import SqliteMemoryStore
from whale_core.plugins.manager import PluginManager
from whale_core.plugins.registry import PluginRegistry

DEFAULT_CONFIG_DIR = Path.home() / ".whale"
CONFIG_FILENAME = "config.json"
MEMORY_FILENAME = "memory.sqlite3"
MANIFESTS_DIRNAME = "manifests"
RUN_CONVERSATION_ID = "cli"

# 各档位的默认 config.json 内容：只含非敏感信息与 env 引用名。
PROFILE_CONFIGS: dict[str, dict[str, Any]] = {
    "cloud": {
        "profile": "cloud",
        "model": {"plugin": "deepseek", "api_key_env": "DEEPSEEK_API_KEY"},
    },
    "local": {
        "profile": "local",
        "model": {"plugin": "local", "base_url": "http://127.0.0.1:11434/v1"},
    },
}

MANIFESTS_README = """\
whale 插件清单目录（manifests/）
================================

把插件清单 JSON 文件（每个插件一个 *.json）放进本目录，
`whale plugins list` 会加载（不启用）它们；启用用 `whale plugins enable <id>`。

清单格式（对齐 whale_core.plugins.manifest.PluginManifest）：

    {
      "plugin_id": "my_plugin",        // 必填，^[a-z0-9][a-z0-9_-]{1,63}$
      "name": "我的插件",               // 必填
      "version": "0.1.0",              // 必填
      "kind": "output",                // 可选：input/output/channel/processor/model/memory
      "inputs": ["my.event"],          // 输入型：命名空间事件 kind
      "actions": ["ping"],             // 输出型：动作名（禁止 shell/exec/run 等保留名）
      "action_risk": {"ping": "read_only"},
      "provides": ["summarize"],       // provider 型：能力名
      "api_version": "1"               // 清单 API 版本（major 必须与核心一致）
    }

可选顶层键 "config_schema"（JSON Schema 最小子集）用于 `plugins info`
的脱敏视图：properties 中 format: "secret" 的字段与键名含
token/key/secret/password 的值一律以 *** 脱敏。

注意：清单不含任何凭据；密钥只放环境变量 / 宿主私有配置。
"""



# ---------------------------------------------------------------------------
# 公共小工具
# ---------------------------------------------------------------------------


def _config_dir(args: argparse.Namespace) -> Path:
    raw = getattr(args, "config_dir", None)
    if raw:
        return Path(raw).expanduser()
    return DEFAULT_CONFIG_DIR


def _ensure_layout(config_dir: Path) -> None:
    """确保 config-dir 与 manifests/ 存在（不触碰 config.json）。"""
    config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(config_dir, 0o700)
    (config_dir / MANIFESTS_DIRNAME).mkdir(exist_ok=True)


def _open_store(config_dir: Path) -> SqliteMemoryStore:
    return SqliteMemoryStore(config_dir / MEMORY_FILENAME)


def _load_config(config_dir: Path) -> dict[str, Any] | None:
    """读取 config.json；缺失或不可解析返回 None（调用方自行报告）。"""
    from whale_core.configuration import read_config
    if not (config_dir / CONFIG_FILENAME).is_file() and not (config_dir / 'secrets' / 'settings.json').is_file():
        return None
    try:
        return read_config(config_dir)
    except (OSError, ValueError):
        return None



def _build_manager(config_dir: Path) -> PluginManager:
    """默认装配：SqliteMemoryStore + PluginRegistry + 显式清单目录加载。"""
    _ensure_layout(config_dir)
    from whale_core.configuration import read_config
    from whale_core.profiles import assemble_runtime
    config = read_config(config_dir)
    return assemble_runtime(str(config.get('profile') or 'local'), config_dir=str(config_dir)).manager



def _print_load_errors(manager: PluginManager) -> None:
    for err in manager.errors:
        print(f"[warn] {err.get('path', '?')}: {err.get('error', '?')}")


# ---------- whale api ----------

API_TOKEN_FILENAME = "api_token"


def _resolve_api_token(config_dir: Path, env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """解析 API token，返回 (token, 来源说明)。优先级：env > 持久化文件 > 新生成。

    新生成只发生一次：token 打印一次后写入 config-dir/api_token（0600）复用。
    """
    import secrets as _secrets

    env = os.environ if env is None else env
    if (tok := (env.get("WHALE_API_TOKEN") or "").strip()):
        return tok, "环境变量 WHALE_API_TOKEN"
    token_path = config_dir / API_TOKEN_FILENAME
    try:
        existing = token_path.read_text(encoding="utf-8").strip()
        if existing:
            os.chmod(token_path, 0o600)
            return existing, f"配置文件 {token_path}"
    except OSError:
        pass
    token = _secrets.token_hex(24)
    _ensure_layout(config_dir)
    token_path.write_text(token + "\n", encoding="utf-8")
    os.chmod(token_path, 0o600)
    return token, f"新生成（已存入 {token_path}，权限 600）"


def _repo_static_dir() -> Path | None:
    """仓库内 web/dist 构建产物（存在才托管）。"""
    package_static = Path(__file__).resolve().parents[1] / 'static'
    if (package_static / 'index.html').is_file():
        return package_static
    dist = Path(__file__).resolve().parents[3] / "web" / "dist"
    return dist if (dist / "index.html").is_file() else None


def _build_api_server(args: argparse.Namespace) -> tuple[Any, str, str]:
    """装配 API 服务，返回 (server, token, token_source)。"""
    from whale_core.api.server import ApiContext, WhaleApiServer
    from whale_core.profiles import assemble_runtime

    config_dir = _config_dir(args)
    from whale_core.configuration import read_config
    config = read_config(config_dir)
    profile = str(config.get("profile") or "local")
    bundle = assemble_runtime(profile, config_dir=str(config_dir))
    token, token_source = _resolve_api_token(config_dir)
    from whale_core.configuration import SettingsController
    from whale_core.api.studio import StudioServices
    ctx = ApiContext(
        store=bundle.store, manager=bundle.manager,
        orchestrator=bundle.orchestrator, curator=bundle.companion.curator,
        profile=profile, settings=SettingsController(config_dir, bundle), optional=bundle.optional,
        # S5：Studio 服务与 S1 resolver 共用同一 MemoryStore（profiles 装配点）。
        studio=StudioServices.from_store(bundle.store),
    )
    server = WhaleApiServer(
        ctx, host=args.host, port=args.port, token=token,
        static_dir=str(_repo_static_dir() or "") or None,
    )
    for note in bundle.notes:
        print(f"  [profile] {note}")
    return server, token, token_source


def _cmd_api(args: argparse.Namespace) -> int:
    from whale_core.core.lease import RuntimeBusy, RuntimeLease
    try:
        with RuntimeLease(_config_dir(args)):
            return _cmd_api_owned(args)
    except RuntimeBusy:
        print("已有 Whale 服务正在使用该配置目录；请停止它或使用其他目录。", file=sys.stderr)
        return 2


def _cmd_api_owned(args: argparse.Namespace) -> int:
    server, token, token_source = _build_api_server(args)
    print(f"[api] 监听 http://{server.host}:{server.port}/api/v1")
    print(f"[api] token 来源：{token_source}")
    if token_source.startswith("新生成"):
        print(f"[api] 新 token（只显示这一次）：{token}")
    if _repo_static_dir() is None:
        print("[api] 提示：web/dist 未构建（pnpm -C web build），控制台静态页不可用，API 正常")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("[api] ⚠️ 监听非回环地址——请确认有 TLS 反代与鉴权前置（roadmap §5.4）")
    print("[api] Ctrl+C 停止")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[api] 已停止")
    finally:
        server.stop()
        server.ctx.store.close()
    return 0



def _status_row(manager: PluginManager, plugin_id: str) -> dict[str, Any] | None:
    for row in manager.status():
        if row["plugin_id"] == plugin_id:
            return row
    return None


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------


def _cmd_setup(args: argparse.Namespace) -> int:
    config_dir = _config_dir(args)
    interactive = (not args.non_interactive) and sys.stdin.isatty()

    profile = args.profile
    if profile is None:
        if interactive:
            answer = input("选择档位 [cloud/local]（默认 local）：").strip().lower()
            if not answer:
                profile = "local"
            elif answer in PROFILE_CONFIGS:
                profile = answer
            else:
                print(f"错误：未知档位 {answer!r}（可选 cloud/local）", file=sys.stderr)
                return 2
        else:
            profile = "local"

    config_path = config_dir / CONFIG_FILENAME
    if (config_path.exists() or (config_dir / 'secrets' / 'settings.json').exists()) and not args.force:
        if interactive:
            answer = input(f"{config_path} 已存在，覆盖？[y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                print("保留已有配置，未改动。")
                return 0
        else:
            print(f"配置已存在：{config_path}（未覆盖；如需重写请加 --force）")
            return 0

    config = dict(PROFILE_CONFIGS[profile])
    if profile == "cloud":
        env_name = config["model"]["api_key_env"]
        if not os.environ.get(env_name):
            print(
                f"错误：cloud 档需要环境变量 {env_name}，请先设置后再运行 setup",
                file=sys.stderr,
            )
            return 2

    _ensure_layout(config_dir)
    manifests_dir = config_dir / MANIFESTS_DIRNAME
    manifests_dir.mkdir(exist_ok=True)
    readme = manifests_dir / "README.txt"
    if not readme.exists():
        readme.write_text(MANIFESTS_README, encoding="utf-8")
    from whale_core.configuration import atomic_json, private_settings, read_config
    previous = read_config(config_dir)
    private = private_settings(config_dir)
    if previous.get('profile') != profile:
        private.pop('model_api_key', None)
    if (config_dir / 'secrets' / 'settings.json').exists():
        atomic_json(config_dir / 'secrets' / 'settings.json', {
            'schema_version': 1, 'config': config, 'private': private})
    atomic_json(config_path, config)
    _open_store(config_dir).close()  # 建库（含表结构），setup 后即可用
    print(f"已写入 {config_path}（profile={profile}）")
    print(f"记忆库：{config_dir / MEMORY_FILENAME}")
    print(f"插件清单目录：{manifests_dir}（格式见 README.txt）")
    if profile == "local":
        print("local 档无需 API key；`whale run` 连接本地模型端点（默认 127.0.0.1:11434，先确认它在跑）。")
    elif profile == "cloud":
        env_name = config["model"]["api_key_env"]
        print(f"cloud 档将从环境变量 {env_name} 读取 key（不会写入配置文件）。")
    return 0


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


class _Reporter:
    """[ok]/[fail]/[warn] 逐行报告；fail 计数决定退出码。

    ``checks`` 同步收集结构化结果供 ``--json`` 机器可读输出
    （只含状态与说明文字，绝不含 key/token/用户正文）。
    """

    def __init__(self, *, quiet: bool = False) -> None:
        self.failures = 0
        self.checks: list[dict] = []
        self._quiet = quiet

    def _emit(self, status: str, msg: str, *, live: bool = False) -> None:
        self.checks.append({"status": status, "detail": msg, "live": live})
        if not self._quiet:
            print(f"[{status}] {msg}")

    def ok(self, msg: str, *, live: bool = False) -> None:
        self._emit("ok", msg, live=live)

    def fail(self, msg: str, *, live: bool = False) -> None:
        self.failures += 1
        self._emit("fail", msg, live=live)

    def warn(self, msg: str, *, live: bool = False) -> None:
        self._emit("warn", msg, live=live)


def _deep_probe_models(base_url: str, api_key: str, *, timeout: float = 5.0) -> tuple[bool, str]:
    """--deep 显式付费级检查：GET {base}/models（免费端点，不产生生成费用）。

    直连不走代理；返回 (ok, detail)。detail 只含 HTTP 状态码/错误类型名。
    """
    import urllib.error
    import urllib.request

    url = base_url.rstrip("/") + "/models"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as resp:
            return 200 <= resp.status < 300, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except Exception as exc:
        return False, type(exc).__name__


def _cmd_doctor(args: argparse.Namespace) -> int:
    as_json = bool(getattr(args, "json", False))
    deep = bool(getattr(args, "deep", False))
    config_dir = _config_dir(args)
    rep = _Reporter(quiet=as_json)

    # 1. Python 版本
    vi = sys.version_info
    if vi >= (3, 11):
        rep.ok(f"python {vi.major}.{vi.minor}.{vi.micro} >= 3.11")
    else:
        rep.fail(f"python {vi.major}.{vi.minor}.{vi.micro} < 3.11")

    # 2. config.json 存在且可解析
    config_path = config_dir / CONFIG_FILENAME
    config: dict[str, Any] | None = None
    if not config_path.is_file():
        rep.fail(f"config.json 不存在：{config_path}（先运行 whale setup）")
    else:
        config = _load_config(config_dir)
        if config is None:
            rep.fail(f"config.json 不可解析：{config_path}")
        else:
            rep.ok(f"config.json 可解析（profile={config.get('profile', '?')}）")

    # 3. 记忆库可打开可写
    db_path = config_dir / MEMORY_FILENAME
    store: SqliteMemoryStore | None = None
    if not db_path.is_file():
        rep.fail(f"记忆库不存在：{db_path}（先运行 whale setup）")
    else:
        try:
            store = _open_store(config_dir)
            store.put("doctor:probe", {"ok": True})
            store.get("doctor:probe")
            store.delete("doctor:probe")
            rep.ok(f"记忆库可打开可写：{db_path}")
        except Exception as exc:
            rep.fail(f"记忆库不可用：{type(exc).__name__}")

    # 4. 模型 provider 配置状态（绝不回显 key 本体）
    if config is None:
        rep.warn("模型 provider 状态未知（config.json 不可用）")
    else:
        profile = config.get("profile", "")
        model = config.get("model") or {}
        if profile == "cloud":
            env_name = str(model.get("api_key_env") or "DEEPSEEK_API_KEY")
            from whale_core.configuration import configured_env
            effective = configured_env(profile, config_dir, os.environ)
            if effective.get('DEEPSEEK_API_KEY'):
                rep.ok(f"模型 provider：cloud 档 key 已配置（env {env_name}）")
            else:
                rep.fail(f"模型 provider：cloud 档 key 未配置（请设置环境变量 {env_name}）")
        elif profile == "local":
            base_url = str(model.get("base_url") or "")
            if base_url:
                rep.ok(f"模型 provider：local 档端点 {base_url}")
            else:
                rep.fail("模型 provider：local 档缺少 model.base_url 配置")
        else:
            rep.warn(f"未知 profile：{profile!r}")

    # 5. manifests 目录可读
    manifests_dir = config_dir / MANIFESTS_DIRNAME
    if manifests_dir.is_dir():
        rep.ok(f"manifests 目录可读：{manifests_dir}")
    else:
        rep.fail(f"manifests 目录不存在：{manifests_dir}（先运行 whale setup）")

    # 6. 已加载插件健康状态
    if store is not None and manifests_dir.is_dir():
        manager = PluginManager(PluginRegistry(), store)
        manager.load_dir(manifests_dir)
        for err in manager.errors:
            rep.warn(f"插件清单 {err.get('path', '?')}: {err.get('error', '?')}")
        rows = manager.status()
        if not rows:
            rep.ok("插件：无已加载插件")
        for row in rows:
            health = manager.healthcheck(row["plugin_id"])
            status = health["status"]
            msg = f"插件 {row['plugin_id']}：{status}（{health['detail']}）"
            if status == "healthy":
                rep.ok(msg)
            elif status in ("unknown", "disabled"):
                rep.warn(msg)
            else:
                rep.fail(msg)

    # 7. --deep：显式同意后才做真实网络连通检查（免费 /models 端点；
    # 默认 doctor 不触网、不触付费操作）。
    if deep:
        effective_cfg = config or {}
        profile = str(effective_cfg.get("profile", ""))
        if profile == "cloud":
            from whale_core.configuration import configured_env
            effective = configured_env(profile, config_dir, os.environ)
            key = effective.get("DEEPSEEK_API_KEY", "")
            model_cfg = effective_cfg.get("model") or {}
            base = str(model_cfg.get("base_url")
                       or effective.get("DEEPSEEK_BASE_URL")
                       or "https://api.deepseek.com/v1")
            if not key:
                rep.warn("deep：模型端点真实探测跳过（key 未配置）", live=False)
            else:
                ok, detail = _deep_probe_models(base, key)
                # 只报告状态码/错误类型，不回显任何凭据
                (rep.ok if ok else rep.fail)(
                    f"deep：模型端点 {base} 真实连通（{ok and '可达' or detail}）"
                    if ok else f"deep：模型端点 {base} 探测失败（{detail}）",
                    live=True)
        else:
            rep.warn("deep：local 档无云端探测对象（端点即本机服务）")

    if as_json:
        import json as _json
        print(_json.dumps({"ok": rep.failures == 0, "deep": deep,
                           "checks": rep.checks}, ensure_ascii=False, indent=2))
    return 1 if rep.failures else 0


# ---------------------------------------------------------------------------
# run（REPL，真实模型端点）
# ---------------------------------------------------------------------------


def _cmd_run(args: argparse.Namespace) -> int:
    from whale_core.core.lease import RuntimeBusy, RuntimeLease
    try:
        with RuntimeLease(_config_dir(args)):
            return _cmd_run_owned(args)
    except RuntimeBusy:
        print("已有 Whale 服务正在使用该配置目录；请停止它或使用其他目录。", file=sys.stderr)
        return 2


def _cmd_run_owned(args: argparse.Namespace) -> int:
    config_dir = _config_dir(args)
    _ensure_layout(config_dir)
    from whale_core.profiles import assemble_runtime
    config = _load_config(config_dir) or {}
    profile = str(config.get("profile") or "local")
    try:
        bundle = assemble_runtime(profile, config_dir=str(config_dir))
    except ValueError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    store = bundle.store
    orchestrator = bundle.orchestrator
    print(f"whale {profile} REPL —— 输入 /quit 退出（真实模型端点）")
    for note in bundle.notes:
        print(f"  [profile] {note}")
    try:
        while True:
            try:
                line = input("you> ")
            except EOFError:
                print()  # EOF 后补换行，保持终端整洁
                break
            text = line.strip()
            if not text:
                continue
            if text in ("/quit", "/exit"):
                print("bye")
                return 0
            try:
                result = orchestrator.handle_message(
                    RUN_CONVERSATION_ID, text, source="cli"
                )
                print(f"whale> {result.reply}")
            except Exception as exc:
                # 不泄露供应商原始错误文本（可能包含敏感配置）。
                kind = getattr(exc, "kind", type(exc).__name__)
                print(f"whale> [error] 模型/工具调用失败：{kind}")
    finally:
        store.close()
    return 0


# ---------------------------------------------------------------------------
# plugins 子命令
# ---------------------------------------------------------------------------


def _cmd_plugins_list(args: argparse.Namespace) -> int:
    manager = _build_manager(_config_dir(args))
    _print_load_errors(manager)
    rows = manager.status()
    header = f"{'ID':<24} {'NAME':<24} {'VERSION':<10} {'KIND':<10} STATUS"
    print(header)
    print("-" * len(header))
    for row in rows:
        print(
            f"{row['plugin_id']:<24} {row['name']:<24} "
            f"{row['version']:<10} {row['kind']:<10} {row['status']}"
        )
    if not rows:
        print("(no plugins loaded)")
    return 0


def _cmd_plugins_info(args: argparse.Namespace) -> int:
    manager = _build_manager(_config_dir(args))
    _print_load_errors(manager)
    row = _status_row(manager, args.plugin_id)
    if row is None:
        print(f"错误：未知插件 {args.plugin_id!r}（未加载）", file=sys.stderr)
        return 1
    print(f"plugin_id : {row['plugin_id']}")
    print(f"name      : {row['name']}")
    print(f"version   : {row['version']}")
    print(f"kind      : {row['kind']}")
    print(f"status    : {row['status']}")
    print(f"api       : {row['api_version']}")
    print(f"inputs    : {', '.join(row['inputs']) or '-'}")
    print(f"actions   : {', '.join(row['actions']) or '-'}")
    print(f"provides  : {', '.join(row['provides']) or '-'}")
    view = manager.config_view(args.plugin_id)  # 脱敏视图：secret 一律 ***
    print("config    :")
    print(json.dumps(view, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def _cmd_plugins_enable(args: argparse.Namespace) -> int:
    manager = _build_manager(_config_dir(args))
    _print_load_errors(manager)
    try:
        manager.enable(args.plugin_id)
    except KeyError:
        print(f"错误：未知插件 {args.plugin_id!r}（未加载）", file=sys.stderr)
        return 1
    row = _status_row(manager, args.plugin_id)
    status = row["status"] if row else "unknown"
    print(f"插件 {args.plugin_id}：{status}")
    return 0 if status == "enabled" else 1


def _cmd_plugins_disable(args: argparse.Namespace) -> int:
    manager = _build_manager(_config_dir(args))
    _print_load_errors(manager)
    if _status_row(manager, args.plugin_id) is None:
        print(f"错误：未知插件 {args.plugin_id!r}（未加载）", file=sys.stderr)
        return 1
    manager.disable(args.plugin_id)
    print(f"插件 {args.plugin_id}：disabled")
    return 0


def _cmd_plugins_doctor(args: argparse.Namespace) -> int:
    manager = _build_manager(_config_dir(args))
    _print_load_errors(manager)
    health = manager.healthcheck(args.plugin_id)
    print(
        f"插件 {health['plugin_id']}：{health['status']}（{health['detail']}）"
    )
    return 0 if health["status"] in ("healthy", "unknown", "disabled") else 1


# ---------------------------------------------------------------------------
# memory 子命令
# ---------------------------------------------------------------------------


def _cmd_memory_export(args: argparse.Namespace) -> int:
    config_dir = _config_dir(args)
    _ensure_layout(config_dir)
    store = _open_store(config_dir)
    out_path = Path(args.path)
    try:
        with out_path.open("w", encoding="utf-8") as file_obj:
            count = export_jsonl(store, file_obj)
    except OSError as exc:
        print(f"错误：无法写入 {out_path}：{exc}", file=sys.stderr)
        return 1
    finally:
        store.close()
    print(f"exported {count} records -> {out_path}")
    return 0


def _cmd_memory_import(args: argparse.Namespace) -> int:
    config_dir = _config_dir(args)
    _ensure_layout(config_dir)
    in_path = Path(args.path)
    if not in_path.is_file():
        print(f"错误：导入文件不存在：{in_path}", file=sys.stderr)
        return 1
    store = _open_store(config_dir)
    try:
        with in_path.open("r", encoding="utf-8") as file_obj:
            imported, skipped = import_jsonl(
                store, file_obj, overwrite=args.overwrite
            )
    finally:
        store.close()
    print(f"imported {imported}, skipped {skipped}")
    return 0


def _cmd_memory_backup(args: argparse.Namespace) -> int:
    """Restores require exclusive host ownership; SQLite backups remain online-safe."""
    from contextlib import nullcontext
    from whale_core.core.lease import RuntimeBusy, RuntimeLease
    try:
        lock = RuntimeLease(_config_dir(args)) if args.memory_command == "restore" else nullcontext()
        with lock:
            return _cmd_memory_backup_owned(args)
    except RuntimeBusy:
        print("恢复前请先停止使用此目录的 Whale 服务。", file=sys.stderr)
        return 2


def _cmd_memory_backup_owned(args: argparse.Namespace) -> int:
    from whale_core.memory.backup import create_backup, restore_backup
    config_dir = _config_dir(args)
    _ensure_layout(config_dir)
    store = _open_store(config_dir)
    try:
        if args.memory_command == 'backup':
            path = create_backup(store, args.path)
            print(f'backup created: {path}')
        else:
            # Preserve the previous data even when a local operator requests restore.
            previous = Path(store.path).with_suffix('.before-restore.sqlite3')
            create_backup(store, previous)
            result = restore_backup(store, args.path)
            print(f"restored {result['records']} records; previous state: {previous}")
        return 0
    except (OSError, ValueError, TypeError) as exc:
        print(f'backup/restore failed: {type(exc).__name__}', file=sys.stderr)
        return 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# parser 与入口
# ---------------------------------------------------------------------------


def _add_config_dir(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config-dir",
        default=None,
        help="配置目录（默认 ~/.whale/），内含 config.json / memory.sqlite3 / manifests/",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whale",
        description="Whale 智能层命令行：setup / doctor / run / plugins / memory",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_setup = sub.add_parser("setup", help="初始化配置目录与 config.json")
    p_setup.add_argument(
        "--profile", choices=sorted(PROFILE_CONFIGS), default=None,
        help="档位：cloud（DeepSeek，需 env key）/ local（本地端点，默认）",
    )
    p_setup.add_argument(
        "--non-interactive", action="store_true",
        help="非交互模式：不提问，直接写默认值（stdin 非 tty 时自动生效）",
    )
    p_setup.add_argument(
        "--force", action="store_true", help="覆盖已存在的 config.json"
    )
    _add_config_dir(p_setup)
    p_setup.set_defaults(func=_cmd_setup)

    p_doctor = sub.add_parser("doctor", help="环境自检（[ok]/[fail]/[warn] 逐行报告）")
    _add_config_dir(p_doctor)
    p_doctor.add_argument("--json", action="store_true",
                          help="机器可读 JSON 输出（供 CI/运维采集）")
    p_doctor.add_argument("--deep", action="store_true",
                          help="显式同意的真实连通检查（免费 /models 端点；默认不触网）")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_run = sub.add_parser("run", help="REPL（使用配置档对应的真实模型端点）")
    _add_config_dir(p_run)
    p_run.set_defaults(func=_cmd_run)

    p_plugins = sub.add_parser("plugins", help="插件管理（显式清单加载）")
    psub = p_plugins.add_subparsers(dest="plugins_command", required=True)
    pl = psub.add_parser("list", help="列出已加载插件")
    _add_config_dir(pl)
    pl.set_defaults(func=_cmd_plugins_list)
    for name, func, helptext in (
        ("info", _cmd_plugins_info, "插件清单摘要 + 脱敏配置视图"),
        ("enable", _cmd_plugins_enable, "启用插件"),
        ("disable", _cmd_plugins_disable, "禁用插件"),
        ("doctor", _cmd_plugins_doctor, "插件健康检查"),
    ):
        p = psub.add_parser(name, help=helptext)
        p.add_argument("plugin_id")
        _add_config_dir(p)
        p.set_defaults(func=func)

    p_desktop = sub.add_parser('desktop', help='启动Linux/macOS桌面控制台')
    _add_config_dir(p_desktop)
    p_desktop.add_argument('--browser', action='store_true', help='使用系统浏览器')
    def desktop(args):
        from whale_core.cli.desktop import run_desktop
        return run_desktop(args.config_dir, browser_only=args.browser)
    p_desktop.set_defaults(func=desktop)

    p_api = sub.add_parser("api", help="启动管理 API + 控制台静态托管（默认 127.0.0.1）")
    p_api.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    p_api.add_argument("--port", type=int, default=8080, help="监听端口（默认 8080）")
    _add_config_dir(p_api)
    p_api.set_defaults(func=_cmd_api)

    # issue #28：QQ 渠道运行器的正式 CLI 入口。参数原样转发给
    # examples/qq_live_workflow.py 的 main（run/approve/show/jobs），
    # 业务逻辑不在 CLI 复制；run 子命令由 whale_core.cli.qq 加单实例锁。
    p_qq = sub.add_parser(
        "qq",
        help="QQ 渠道运行器（whale qq run|approve|show|jobs；"
             "真实发送需 --live-auto-send + QQ_LIVE_AUTOSEND=1 双开关）",
    )
    p_qq.add_argument(
        "qq_args", nargs=argparse.REMAINDER,
        help="原样转发给 QQ 运行器的参数（如 run --auto-approve；"
             "子命令帮助用 whale qq run --help）",
    )

    def qq(args: argparse.Namespace) -> int:
        from whale_core.cli.qq import main as qq_main
        return qq_main(args.qq_args)
    p_qq.set_defaults(func=qq)

    p_memory = sub.add_parser("memory", help="记忆导出 / 导入（JSONL）")
    msub = p_memory.add_subparsers(dest="memory_command", required=True)
    me = msub.add_parser("export", help="导出记忆为 JSONL")
    me.add_argument("path", help="导出目标文件路径")
    _add_config_dir(me)
    me.set_defaults(func=_cmd_memory_export)
    mi = msub.add_parser("import", help="从 JSONL 导入记忆")
    mi.add_argument("path", help="导入源文件路径")
    mi.add_argument(
        "--overwrite", action="store_true", help="同 key 覆盖写入（默认跳过）"
    )
    _add_config_dir(mi)
    mi.set_defaults(func=_cmd_memory_import)

    for operation in ('backup', 'restore'):
        p = msub.add_parser(operation, help='完整SQLite备份 / 恢复（恢复前停止服务）')
        p.add_argument('path', help='SQLite备份文件路径')
        _add_config_dir(p)
        p.set_defaults(func=_cmd_memory_backup)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：解析参数并分派，返回退出码（不自行 sys.exit）。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
