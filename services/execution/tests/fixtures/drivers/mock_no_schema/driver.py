"""Mock Management driver that does NOT publish a config schema.

This is the backward-compat case: every existing driver omits config_schema(),
so the __config_schema__ sentinel must return {"has_schema": False} and the
validation path must fall back to the hardcoded registry. See
docs/design/0002-driver-published-config-schemas.md.

Contract: Management (login, logout, configure, backup, status). See
docs/DRIVERS.md.
"""


class Driver:
    """Management driver, mock backend, with no published schema."""

    def __init__(self, context):
        self.context = context

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
