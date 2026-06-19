"""Safe JSON writing for the dashboards.

Python's json happily emits `NaN`/`Infinity` (and reads them back), but a
browser's JSON.parse REJECTS them — which silently blanks the page. Everything
written for the dashboard goes through here so a stray NaN can never ship.
"""
from __future__ import annotations

import json
import math


def _clean(o):
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _clean(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_clean(v) for v in o]
    return o


def dump(obj, path):
    """Write obj to path as browser-parseable JSON (NaN/Inf -> null)."""
    path.write_text(json.dumps(_clean(obj), indent=2, allow_nan=False))
