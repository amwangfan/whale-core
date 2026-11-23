"""Build ONE self-contained demo page for README screenshots: tests/out/demo.html.

    python tests/demo.py            (from web/, or any cwd — paths are relative to this file)

The page works from file:// with no server: inline CSS (styles.css with its @imports resolved),
one classic inline <script> holding the vendored preact-htm (ESM export rewritten) and every
app module as a factory (same transform as tests/bundle.py), then the demo data
(mocks-groups.js + demo-data.js) and tests/demo-runtime.js, which installs a fake `fetch` for
/api/rpc/* and a fake WebSocket for /api/link, pre-seeds the token and picks the page.

Open a page directly:  demo.html?page=bridge|chat|memory|groups|tasks|capabilities|devices|persona|settings|setup
or with a route:       demo.html#/memory/m_cat     (extra: &theme=light|dark, &view=list, &dim=3d, &sources=1)

The console's CSP does not apply to this artifact (it is never served by `whale serve`).
"""

from __future__ import annotations

import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.dirname(HERE)

_ns: dict = {}
exec(compile(open(os.path.join(HERE, "bundle.py"), encoding="utf-8").read(), "bundle.py", "exec"), _ns)
transform = _ns["transform"]


def read(*parts: str) -> str:
    return open(os.path.join(WEB, *parts), encoding="utf-8").read()


def inline_css() -> str:
    entry = read("app", "styles.css")

    def sub(m: re.Match) -> str:
        return f"/* {m.group(1)} */\n" + read("app", m.group(1))

    return re.sub(r'@import\s+url\("\./([^"]+)"\);', sub, entry)


def vendor_factory() -> str:
    src = read("vendor", "preact-htm.js")
    m = re.search(r"export\{([^}]*)\};?\s*$", src)
    if not m:
        raise SystemExit("vendor export list not found")
    pairs = []
    for item in m.group(1).split(","):
        local, _, name = item.strip().partition(" as ")
        pairs.append(f"{json.dumps(name.strip() or local.strip())}: {local.strip()}")
    body = src[: m.start()] + "Object.assign(__exports, {" + ", ".join(pairs) + "});"
    return "__modules['vendor'] = function (__exports) {\n" + body + "\n};\n"


LOADER = r"""
var __modules = Object.create(null);
var __cache = Object.create(null);
function __req(id) {
  if (__cache[id]) return __cache[id];
  var f = __modules[id];
  if (!f) throw new Error('module not found: ' + id);
  var ex = {};
  __cache[id] = ex;
  f(ex);
  return ex;
}
"""


def app_modules() -> str:
    out = []
    app = os.path.join(WEB, "app")
    mods = []
    for root, _, files in os.walk(app):
        for f in sorted(files):
            if f.endswith(".js"):
                mods.append(os.path.relpath(os.path.join(root, f), WEB).replace("\\", "/"))
    for mod_id in sorted(mods):
        body, _ = transform(read(mod_id), mod_id)
        out.append(f"__modules[{json.dumps(mod_id)}] = function (__exports) {{ 'use strict';\n{body}\n}};\n")
    return "".join(out)


def icon_data_uri() -> str:
    from urllib.parse import quote

    return "data:image/svg+xml," + quote(read("app", "icon.svg").strip())


def build(out_path: str | None = None) -> dict:
    script = "(function () {\n" + LOADER + vendor_factory() + app_modules() + read("tests", "mocks-groups.js") + "\n" \
        + read("tests", "demo-data.js") + "\n" + read("tests", "demo-runtime.js") + "\nglobalThis.__whaleReq = __req;\n__req('app/main.js');\n})();\n"
    script = script.replace("</script", "<\\/script")
    css = inline_css().replace("</style", "<\\/style")
    html = f"""<!doctype html>
<html lang="zh-CN" data-theme="auto">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="dark light">
<title>鲸鱼 · Whale 控制台（演示）</title>
<link rel="icon" type="image/svg+xml" href="{icon_data_uri()}">
<style>
{css}
</style>
</head>
<body>
<div id="app" class="boot" aria-live="polite"><div class="boot-splash" role="status"><div class="boot-orb" aria-hidden="true"></div><p>正在唤醒鲸鱼…</p></div></div>
<script>
{script}
</script>
</body>
</html>
"""
    out = out_path or os.path.join(HERE, "out", "demo.html")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    open(out, "w", encoding="utf-8").write(html)
    # the same script on MiniDOM (real Preact, no browser): tests/out/demo-dom.js — run it with run_javascript
    # The verification sandbox is a hardened realm (Object.prototype frozen), where Preact's internal
    # assign() trips the "override mistake" on vnodes' own `constructor` key during re-renders. The check
    # build alone swaps that helper for a defineProperty copy; demo.html keeps the vendor code untouched.
    assign = "function s(e,n){for(var _ in n)e[_]=n[_];return e}"
    safe = "function s(e,n){for(var _ in n)Object.defineProperty(e,_,{value:n[_],writable:!0,enumerable:!0,configurable:!0});return e}"
    check = read("tests", "minidom.js") + "\n" + script.replace(assign, safe, 1) + "\n" + read("tests", "demo-dom.js")
    open(os.path.join(HERE, "out", "demo-dom.js"), "w", encoding="utf-8").write(check)
    return {"path": out, "bytes": len(html.encode("utf-8")), "script_chars": len(script), "css_chars": len(css)}


if __name__ == "__main__":
    print(json.dumps(build(), ensure_ascii=False))
