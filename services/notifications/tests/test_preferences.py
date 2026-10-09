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


# Every event key that existed before issue #1077 added reservation.failed. A
# user who saved preferences then has exactly these keys stored, because the
# PUT proxy writes the merged defaults back (INTEG-PREFS-4).
_PRE_1077_STORED = {
    "channels": {"in_app": True, "email": False, "chat": False, "webhook": False},
    "events": {
        "reservation.created": True,
        "reservation.updated": False,
        "reservation.cancelled": True,
        "reservation.completed": False,
        "device.health_transition": False,
        "reservation.expiring_soon": True,
    },
}


class TestReservationFailedDefault:
    """Issue #1077: reservation.failed is a default-on event key."""

    def test_failed_is_a_default_event_type(self):
        assert "reservation.failed" in DEFAULT_EVENT_TYPES

    def test_stored_preferences_predating_the_key_receive_it_on(self):
        prefs = NotificationPreferences.with_defaults(_PRE_1077_STORED)
        assert prefs.events["reservation.failed"] is True
        assert prefs.event_enabled("reservation.failed") is True
        # Every stored choice is kept exactly as it was.
        for key, value in _PRE_1077_STORED["events"].items():
            assert prefs.events[key] is value

    def test_explicit_opt_out_of_failed_is_kept(self):
        stored = {"events": {**_PRE_1077_STORED["events"], "reservation.failed": False}}
        prefs = NotificationPreferences.with_defaults(stored)
        assert prefs.events["reservation.failed"] is False
        assert prefs.event_enabled("reservation.failed") is False

    def test_with_defaults_does_not_mutate_the_stored_dict(self):
        stored = {"events": dict(_PRE_1077_STORED["events"])}
        NotificationPreferences.with_defaults(stored)
        assert "reservation.failed" not in stored["events"]
