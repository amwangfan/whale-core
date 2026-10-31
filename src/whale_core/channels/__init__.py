"""渠道域（channels）：外部消息渠道的统一入口命名空间（issue #27）。

当前正式收录 QQ 渠道：``whale_core.channels.qq``。

定位约定：本子包是面向使用方的**正式领域入口**；渠道实现模块仍位于
``whale_core.life``（如 ``qq_onebot`` / ``qq_workflow``）且原位不动，
既有 import 路径全部保持有效——``channels.qq`` re-export 的是**同一批对象**
（``is`` 恒等），不是副本。
"""

from __future__ import annotations

__all__ = ["qq"]
