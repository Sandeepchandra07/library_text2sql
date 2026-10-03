from __future__ import annotations

import logging
import threading

from .schema_kb import SchemaKB

log = logging.getLogger(__name__)
_kb: SchemaKB | None = None
_lock = threading.Lock()


def get_kb() -> SchemaKB:
    global _kb
    if _kb is None:
        with _lock:
            if _kb is None:
                _kb = _build()
    return _kb


def reload_kb() -> SchemaKB:
    global _kb
    with _lock:
        _kb = _build()
    return _kb


def _build() -> SchemaKB:
    catalog = None
    try:
        from .db import fetch_catalog
        catalog = fetch_catalog()
        log.info("Live catalog loaded: %d tables", len(catalog))
    except Exception as e:  # noqa: BLE001
        log.warning("DB catalog unavailable (%s) - running with Excel schema only", e)
    return SchemaKB.load(catalog=catalog)
