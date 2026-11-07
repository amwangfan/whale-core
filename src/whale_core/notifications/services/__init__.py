"""通知业务服务层：NotificationService（唯一业务入口）+ NotificationHistory。"""

from whale_core.notifications.services.notification_history import (
    DEFAULT_HISTORY_LIMIT,
    NotificationHistory,
)
from whale_core.notifications.services.notification_service import (
    DEFAULT_APPROVAL_ACTIONS,
    NotificationService,
)

__all__ = [
    "DEFAULT_APPROVAL_ACTIONS",
    "DEFAULT_HISTORY_LIMIT",
    "NotificationHistory",
    "NotificationService",
]
