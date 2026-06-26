import logging

from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

DEFAULT_EVENT_TYPES = (
    "reservation.created",
    "reservation.updated",
    "reservation.cancelled",
    "reservation.completed",
    # ROADMAP #13 iter 2: device health-transition events fan out to admins
    # and active reservation holders. User toggle is fleet-wide; per-device
    # opt-out is a future iter.
    "device.health_transition",
    # ROADMAP #40: upcoming-expiry reminder, emitted by the reservations
    # expiration task within a configurable lead window of end_time.
    "reservation.expiring_soon",
)


class NotificationChannels(BaseModel):
    """Per-user channel opt-in.

    in_app defaults on (unchanged behavior). The outbound channels (email,
    chat, webhook) default off so a user hears nothing on a new channel until
    they explicitly opt in, per the issue's "outbound channels default off".
    """

    in_app: bool = True
    email: bool = False
    chat: bool = False
    webhook: bool = False


class NotificationPreferences(BaseModel):
    channels: NotificationChannels = Field(default_factory=NotificationChannels)
    events: dict[str, bool] = Field(default_factory=dict)

    def event_enabled(self, event_type: str) -> bool:
        return self.events.get(event_type, True)

    def channel_enabled(self, channel: str) -> bool:
        return bool(getattr(self.channels, channel, True))

    @classmethod
    def with_defaults(cls, stored: dict | None) -> "NotificationPreferences":
        if stored is not None and not isinstance(stored, dict):
            logger.warning(
                "Ignoring non-dict stored notification preferences (%s); using defaults",
                type(stored).__name__,
            )
            stored = None
        try:
            prefs = cls.model_validate(dict(stored)) if stored else cls()
        except ValidationError:
            logger.warning("Ignoring malformed stored notification preferences; using defaults")
            prefs = cls()
        for event_type in DEFAULT_EVENT_TYPES:
            prefs.events.setdefault(event_type, True)
        return prefs


class NotificationPreferencesUpdate(BaseModel):
    channels: NotificationChannels | None = None
    events: dict[str, bool] | None = None
