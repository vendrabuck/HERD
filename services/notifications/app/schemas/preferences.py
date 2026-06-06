from pydantic import BaseModel, Field

DEFAULT_EVENT_TYPES = (
    "reservation.created",
    "reservation.updated",
    "reservation.cancelled",
    "reservation.completed",
    # ROADMAP #13 iter 2: device health-transition events fan out to admins
    # and active reservation holders. User toggle is fleet-wide; per-device
    # opt-out is a future iter.
    "device.health_transition",
)


class NotificationChannels(BaseModel):
    in_app: bool = True


class NotificationPreferences(BaseModel):
    channels: NotificationChannels = Field(default_factory=NotificationChannels)
    events: dict[str, bool] = Field(default_factory=dict)

    def event_enabled(self, event_type: str) -> bool:
        return self.events.get(event_type, True)

    def channel_enabled(self, channel: str) -> bool:
        return bool(getattr(self.channels, channel, True))

    @classmethod
    def with_defaults(cls, stored: dict | None) -> "NotificationPreferences":
        data = dict(stored or {})
        prefs = cls.model_validate(data) if data else cls()
        for event_type in DEFAULT_EVENT_TYPES:
            prefs.events.setdefault(event_type, True)
        return prefs


class NotificationPreferencesUpdate(BaseModel):
    channels: NotificationChannels | None = None
    events: dict[str, bool] | None = None
