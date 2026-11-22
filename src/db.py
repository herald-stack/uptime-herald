# Uptime Herald - https://uptimeherald.org
# Copyright (C) 2026 Uptime Herald Authors
# SPDX-License-Identifier: AGPL-3.0-only
"""
db.py - Picks the MariaDB or PostgreSQL database backend.

Picks between db_mariadb.py and db_postgres.py at import time, based on
the DB_BACKEND environment variable, and makes `src.db` literally BE
whichever one was selected -- not a re-exporting wrapper around it.

Why aliasing rather than `from src.db_mariadb import *`: db_mariadb.py
and db_postgres.py each track their own connection pool in a
module-level `_pool` global, and the test suite monkeypatches that
directly (`monkeypatch.setattr(db, "_pool", FakePool(...))`) expecting
the real load_state()/save_state()/etc. functions to see the patched
value. A `from ... import *` re-export would copy `_pool`'s value into
this module's own separate namespace at import time -- patching `db._pool`
afterward would touch a completely different attribute than the one the
backend's own functions actually read via `global _pool`, silently
breaking every test that patches the pool. Making `src.db` and (e.g.)
`src.db_mariadb` the exact same module object in sys.modules means
`db._pool`, `db.load_state`, `db.MonitorState`, all of it, are the same
attributes either way -- there's no copy to drift out of sync.

DB_BACKEND is read once, at first import -- like the rest of this app's
DB_* configuration, it's an infrastructure setting for docker-compose.yaml,
not something meant to change at runtime without a restart.
"""

import logging
import os
import sys
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    # Static-analysis view of the swap below.
    #
    # `sys.modules[__name__] = _selected_backend` is invisible to a type
    # checker -- it can't know that `from src import db` then yields a
    # different module -- so every `db.load_state(...)`, `db.save_state(...)`
    # and so on would read as an attribute that doesn't exist here.
    #
    # This branch tells the checker what actually happens at runtime:
    # src.db's public surface IS a backend's public surface. db_mariadb is
    # named as the reference because the two backends are required to be
    # interchangeable -- which is not an assumption, it's asserted by
    # test_db_dispatcher.py's test_dispatched_module_exposes_full_expected_
    # surface, which imports both under each DB_BACKEND value and checks the
    # same names exist on whichever got selected. If the two ever diverge,
    # that test fails before this becomes misleading.
    #
    # Never executed at runtime (TYPE_CHECKING is False), so the aliasing
    # below remains the only thing that has any effect, and the
    # monkeypatching semantics the docstring describes are untouched.
    from src.db_mariadb import *  # noqa: F403
    from src.db_mariadb import _pool  # noqa: F401  (tests patch this directly)

_BACKEND = os.environ.get("DB_BACKEND", "mariadb").strip().lower()

_selected_backend: object

if _BACKEND == "mariadb":
    from src import db_mariadb as _selected_backend  # type: ignore[no-redef]
elif _BACKEND in ("postgresql", "postgres"):
    from src import db_postgres as _selected_backend  # type: ignore[no-redef]
else:
    raise ValueError(
        f"Unknown DB_BACKEND: {_BACKEND!r} -- expected 'mariadb' or "
        f"'postgresql' (set via the DB_BACKEND environment variable)"
    )

logger.info("DB backend selected: %s", _BACKEND)

sys.modules[__name__] = _selected_backend  # type: ignore[assignment]
