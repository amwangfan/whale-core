"""泛化插件层：新输入 / 输出 / 内部处理点不改核心（微信、桌面、邮件、纪要、意图委托……）。

六类插件（PluginKind）：

- **input**：只产事件（``PluginEvent``，PerceptionEvent 子类）——前台监测、邮件接收、
  日程、刷视频、日常语音、雷鸟纪要；
- **output**：只执行动作（``PluginAction`` → ``PluginExecutor``）——发邮件、桌面通知、购物；
- **channel**：输入输出多端共用——QQ、微信、通话桥；
- **processor / model / memory**（provider 类）：内部处理（意图委托执行、摘要）、
  模型端点供给、记忆/存储后端——无 inputs/actions，按 ``provides`` 能力名解析。

门控：``PluginActionPolicy`` 按 read_only / communicate / transaction 分级；
执行：``PluginGateway``（策略内嵌的单一执行点），可挂进 CompositeActionGateway；
委托：``ProcessorRouter`` + ``DelegatingIntentProcessor``（DeepSeek 产意图 →
qwen 等执行器模型跑工具循环 → 只回摘要，上下文隔离）。

不做自动发现：注册永远显式（宿主组装代码调用 ``PluginRegistry.register``）；
不可信插件用外置进程（QQ OneBot / WeQ 适配器先例），核心不 import 插件业务代码。
**门控本身不可插件化**：Initiative / ShellPolicy / OwnerChannelRegistry /
PluginActionPolicy 是核心安全不变量。

三层职责（issue #31；模块级分类与已知例外见本目录 README.md）：

- **framework（插件框架契约）**：manifest / registry / loader / manager /
  gateway / policy / events（+ intents / processing 契约）——插件与核心
  之间的稳定契约面；framework 模块不得 import 内置 provider 实现；
- **builtin providers（内置 provider 实现）**：本包内的媒体 / 语音 /
  shell / realtime / 模型供给等能力实现——走同一框架注册，但**不是**
  框架契约，可按需替换；
- **bundled official plugins（捆绑官方插件）**：仓库根 ``plugins/`` 下的
  weq_channel / search_web——独立发行单元，随 wheel 打包（pyproject.toml
  ``[tool.hatch.build.targets.wheel] packages`` 显式列入），经
  ``plugins/loader.py`` 显式加载，核心不 import 其业务代码。
"""

from whale_core.plugins.events import PluginEvent
from whale_core.plugins.gateway import PluginGateway
from whale_core.plugins.intents import PluginAction
from whale_core.plugins.manifest import (
    CORE_EVENT_KINDS,
    FORBIDDEN_ACTION_NAMES,
    PROVIDER_KINDS,
    PluginKind,
    PluginManifest,
    RiskClass,
)
from whale_core.plugins.policy import PluginActionPolicy, PluginVerdict
from whale_core.plugins.processing import (
    DelegatingIntentProcessor,
    ExecutorModelClient,
    IntentProcessor,
    NullIntentProcessor,
    ProcessingRequest,
    ProcessingResult,
    ProcessorRouter,
)
from whale_core.plugins.registry import (
    NullPluginExecutor,
    PluginExecutor,
    PluginRegistration,
    PluginRegistry,
)

__all__ = [
    "PluginEvent",
    "PluginAction",
    "PluginManifest",
    "PluginKind",
    "PROVIDER_KINDS",
    "RiskClass",
    "PluginRegistry",
    "PluginRegistration",
    "PluginExecutor",
    "NullPluginExecutor",
    "PluginActionPolicy",
    "PluginVerdict",
    "PluginGateway",
    "ProcessingRequest",
    "ProcessingResult",
    "IntentProcessor",
    "NullIntentProcessor",
    "ProcessorRouter",
    "ExecutorModelClient",
    "DelegatingIntentProcessor",
    "CORE_EVENT_KINDS",
    "FORBIDDEN_ACTION_NAMES",
]
