from app.services.dispatchers.base import Dispatcher, DispatchMessage
from app.services.dispatchers.chat import ChatDispatcher
from app.services.dispatchers.email import EmailDispatcher
from app.services.dispatchers.in_app import InAppDispatcher
from app.services.dispatchers.webhook import WebhookDispatcher


def default_dispatchers() -> list[Dispatcher]:
    """The full set of dispatchers, in delivery order.

    In-app first so a transport stall on an outbound channel never delays the
    bell. The consumer filters this set per recipient by their channel toggles;
    outbound channels default off in preferences, so this set is safe to always
    pass.
    """
    return [
        InAppDispatcher(),
        EmailDispatcher(),
        ChatDispatcher(),
        WebhookDispatcher(),
    ]


__all__ = [
    "ChatDispatcher",
    "Dispatcher",
    "DispatchMessage",
    "EmailDispatcher",
    "InAppDispatcher",
    "WebhookDispatcher",
    "default_dispatchers",
]
