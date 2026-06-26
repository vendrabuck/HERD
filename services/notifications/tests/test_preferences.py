from app.schemas.preferences import DEFAULT_EVENT_TYPES, NotificationPreferences


def _assert_safe_defaults(prefs: NotificationPreferences) -> None:
    assert isinstance(prefs, NotificationPreferences)
    for event_type in DEFAULT_EVENT_TYPES:
        assert prefs.events.get(event_type) is True
    # Channel defaults: in_app on, outbound off.
    assert prefs.channels.in_app is True
    assert prefs.channels.email is False


class TestWithDefaults:
    def test_none_yields_defaults(self):
        _assert_safe_defaults(NotificationPreferences.with_defaults(None))

    def test_valid_dict_validated_and_filled(self):
        prefs = NotificationPreferences.with_defaults(
            {"channels": {"email": True}, "events": {"reservation.created": False}}
        )
        # Explicit value preserved.
        assert prefs.channels.email is True
        assert prefs.events["reservation.created"] is False
        # Unspecified event types still defaulted to True.
        for event_type in DEFAULT_EVENT_TYPES:
            if event_type != "reservation.created":
                assert prefs.events.get(event_type) is True

    def test_non_dict_string_falls_back(self):
        _assert_safe_defaults(NotificationPreferences.with_defaults("garbage"))

    def test_non_dict_list_falls_back(self):
        _assert_safe_defaults(NotificationPreferences.with_defaults([1, 2]))

    def test_non_dict_int_falls_back(self):
        _assert_safe_defaults(NotificationPreferences.with_defaults(123))

    def test_malformed_channels_falls_back(self):
        _assert_safe_defaults(NotificationPreferences.with_defaults({"channels": "not-a-dict"}))

    def test_malformed_events_falls_back(self):
        _assert_safe_defaults(NotificationPreferences.with_defaults({"events": "not-a-dict"}))
