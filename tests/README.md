# tests/ 组织约定与里程碑命名迁移清单（issue #29）

## 组织约定

pytest 默认发现（`pyproject.toml`：`testpaths = ["tests"]`，rootdir 模式，
无 `__init__.py`，因此**全树内测试文件 basename 必须唯一**）。

- `tests/` 根：平铺的功能 / 模块测试（现状主体，本 issue 不搬迁）。
- `tests/unit/`：单组件离线单元测试，可按领域再分子目录（如 `unit/core/`、`unit/operations/`）。
- `tests/integration/`：跨组件链路测试（离线替身 / 内存 store 也算，只要编排多个子系统）。
- `tests/security/`：安全边界回归（密钥屏蔽、信任级别、注入隔离、会话孤儿等）。
- `tests/contract/`：协议 / 线契约测试（预留；当前契约烟测仍在根目录，由
  `.github/workflows/contract-smoke.yml` 显式点名）。

新测试文件命名 `test_<领域>_<主题>.py`，不再使用里程碑名（P0/G2/G4…）。
共享替身集中在 `tests/_doubles.py` 与 `tests/fixtures/`（仅供根目录文件直接
`import _doubles` / `from fixtures...`；子目录文件如需使用，应写全限定导入）。

## 里程碑命名迁移清单（2026-11，issue #29）

仅用 `git mv` 迁移 + 首行 docstring 标注原名，未增删任何测试、未改测试语义。

| 旧路径 | 新路径 | 测试数（前 = 后） |
| --- | --- | --- |
| `tests/test_p0_stubs.py` | `tests/unit/core/test_core_continuity_stubs.py` | 8 = 8 |
| `tests/test_g2_memory_identity.py` | `tests/integration/test_memory_identity_governance.py` | 12 = 12 |
| `tests/test_g4_operations.py` | `tests/unit/operations/test_operations_diagnostics.py` | 11 = 11 |
| `tests/test_g4_trust.py` | `tests/security/test_security_plugin_trust.py` | 24 = 24 |
| `tests/test_review_fixes.py` | `tests/security/test_security_review_fixes.py` | 8 = 8 |
| `tests/test_final_security_review.py` | `tests/security/test_security_credential_sessions.py` | 8 = 8 |
| 合计 | | 71 = 71 |

验证：`pytest tests/security/ tests/unit/ tests/integration/ -q` → 71 passed；
全量 `pytest tests/ -q` 迁移后 1664 passed 全绿（`--collect-only` 计数迁移前后一致；
迁移期间另一代理并行推进 device_bridge / channels 改动，全量绝对数以当时工作区为准）。

同步更新的引用：

- `.github/workflows/contract-smoke.yml`：`tests/test_g2_memory_identity.py`
  → `tests/integration/test_memory_identity_governance.py`。

已知未同步的引用（按归档约定与任务边界有意不改）：

- `docs/g4-operations.md` 第 177 / 202 / 205 行仍引用旧文件名，待该文档维护者顺手更新；
- `docs/archive/2026-10-30/security-review-2026-10-30.md` 为历史快照，不改写。

其余里程碑命名文件（`test_p1_stubs.py`、`test_g3_scenarios.py`、`test_e2e_p0.py`）
不在 issue #29 范围内，留待后续 issue 按同约定迁移。
