"""Mock Management driver that publishes a config schema via config_schema().

Used by test_config_schema_extraction.py to prove the __config_schema__
sentinel action reads the classmethod and returns a plain JSON Schema dict.
The schema is deliberately narrower than the hardcoded Management registry
entry (it omits `ip`) so an override test can show the published schema beats
the registry. See docs/design/0002-driver-published-config-schemas.md.

Contract: Management (login, logout, configure, backup, status). See
docs/DRIVERS.md.
"""


class Driver:
    """Management driver, mock backend, that advertises its config schema."""

    def __init__(self, context):
        self.context = context

    @classmethod
    def config_schema(cls):
        return {
            "type": "object",
            "properties": {
                "vlan": {"type": "integer", "minimum": 1, "maximum": 4094},
                "hostname": {"type": "string", "maxLength": 128},
            },
            "additionalProperties": False,
        }

    def login(self):
        return {"success": True}

    def logout(self):
        return {"success": True}

    def configure(self, **kwargs):
        return {"success": True, "applied": kwargs}

    def backup(self):
        return {"success": True}

    def status(self):
        return {"reachable": True}
