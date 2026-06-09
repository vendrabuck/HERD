"""Mock driver whose __init__ REQUIRES live credentials.

Proves config_schema() is invoked on the class object WITHOUT instantiating
Driver(context). __init__ reads context["HERD_ip_address"] and raises KeyError
when it is absent (the __config_schema__ path passes an empty context), so if
extraction ever instantiated the driver this fixture would crash. The Calient
example in docs/DRIVERS.md:378-383 is the real-world shape of this case.

Contract: Management (login, logout, configure, backup, status). See
docs/DRIVERS.md.
"""


class Driver:
    """Management driver whose __init__ needs a live device address."""

    def __init__(self, context):
        # Hard dependency on a credential-bearing context. The schema
        # extraction path must never reach this.
        self.ip_address = context["HERD_ip_address"]
        self.context = context

    @classmethod
    def config_schema(cls):
        return {
            "type": "object",
            "properties": {
                "hostname": {"type": "string", "maxLength": 64},
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
