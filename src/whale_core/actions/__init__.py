"""Actions：意图 dataclass 与 ActionGateway 协议（stub，不真连）。"""

from whale_core.actions.intents import (
    AmbientListen,
    NavigateHint,
    PhoneAction,
    PlaceCall,
    ShellCommand,
    ShellWhitelist,
    ShowCard,
    Speak,
)
from whale_core.actions.gateway import (
    ActionGateway,
    AmbientListenGateway,
    CompositeActionGateway,
    HttpDashboardGateway,
    LocalBridgeGateway,
    MockHttpActionGateway,
    NullActionGateway,
    PhoneControlGateway,
)

__all__ = [
    "ShowCard",
    "PlaceCall",
    "Speak",
    "PhoneAction",
    "NavigateHint",
    "AmbientListen",
    "ShellWhitelist",
    "ShellCommand",
    "ActionGateway",
    "NullActionGateway",
    "HttpDashboardGateway",
    "LocalBridgeGateway",
    "PhoneControlGateway",
    "AmbientListenGateway",
    "MockHttpActionGateway",
    "CompositeActionGateway",
]
