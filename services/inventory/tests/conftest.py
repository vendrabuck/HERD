"""Shared pytest configuration for services/inventory's test suite.

Several test modules (e.g. ``test_apply_scheduler.py``) build their own
in-memory SQLite engine and call ``Base.metadata.create_all`` in a
module-local fixture rather than going through the ASGI app. ``Base.metadata``
is populated purely as a side effect of importing a model's module, so a test
module that only imports the handful of model classes it exercises directly
can, when run in isolation, define a foreign key that targets a table whose
model class nothing has imported yet: SQLAlchemy raises
``sqlalchemy.exc.NoReferencedTableError`` at ``create_all`` time (issue #727).
The failure is import-order dependent, which is why the same file passes
inside the full suite, once some other, earlier-collected module happens to
have already imported the missing model.

Importing every module under ``app.models`` here, before pytest collects any
test module, registers every model class on the shared ``Base`` up front and
removes that ordering dependency. This walks the package directly rather than
trusting ``app.models.__init__`` to re-export every model: it currently does
not (it omits ``app.models.hypervisor``, the model #727 actually tripped
over), so relying on it here would reintroduce the same latent bug the moment
a new model module was added without also updating that ``__init__``.
"""

import importlib
import pkgutil

import app.models


def _import_all_inventory_models() -> None:
    for module_info in pkgutil.iter_modules(app.models.__path__, prefix=f"{app.models.__name__}."):
        importlib.import_module(module_info.name)


_import_all_inventory_models()
