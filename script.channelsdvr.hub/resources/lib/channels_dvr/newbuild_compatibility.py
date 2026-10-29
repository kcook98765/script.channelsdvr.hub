"""Calendar-validated fixed minimum; persistent association is NOT authentication.

ANCHORv1 retains historical receipt bytes for the same normalized origin and
namespace. It cannot authenticate a physical DVR later reusing that origin.
Unknown generic transport/test builds keep exact comparison and literal hashes.
"""
from datetime import datetime
import hashlib
import json
import re

BASE_MINIMUM_DVR_BUILD = '2026.08.07.0346'
PROFILE_BINDING_ANCHOR_V1 = BASE_MINIMUM_DVR_BUILD  # historical hash component ONLY
_STAMP = re.compile(r'([0-9]{4})\.([0-9]{2})\.([0-9]{2})\.([0-9]{2})([0-9]{2})'
                    r'(?:-(pre|alpha|beta|rc)([0-9]{0,3}))?')
_STAGES = {'pre': 0, 'alpha': 1, 'beta': 2, 'rc': 3, None: 4}


def parse_build(value: object) -> tuple[int, ...] | None:
    """Strict padded timestamp, Gregorian calendar, pre<alpha<beta<rc<final.

    Suffix numbers compare numerically (missing number means0). Prereleases of a
    newer timestamp are later than final builds of an older timestamp.
    """
    if type(value) is not str or not 1 <= len(value) <= 40:
        return None
    match = _STAMP.fullmatch(value)
    if match is None:
        return None
    parts = tuple(int(match[i]) for i in range(1, 6))
    year, month, day, hour, minute = parts
    try:
        datetime(year, month, day, hour, minute)  # real calendar validation, no wall clock
    except ValueError:
        return None
    return parts + (_STAGES[match[6]], int(match[7] or '0'))


def supported_build(value: object) -> bool:
    parsed = parse_build(value)
    return parsed is not None and parsed >= (2026, 8, 7, 3, 46, 4, 0)


def version_admitted(expected_version: object, actual_version: object) -> bool:
    """Date configuration is a legacy field, NOT an operator-raised floor.

    All normal clients use the fixed global minimum. Non-date, explicit opaque
    test transports retain exact equality; malformed dates never take that path.
    The normal application UI never admits opaque configurations.
    """
    if parse_build(expected_version) is not None:
        return supported_build(actual_version)
    return (type(expected_version) is str and type(actual_version) is str
            and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,63}', expected_version) is not None
            and actual_version == expected_version)


def binding_component(expected_version: str) -> str:
    return PROFILE_BINDING_ANCHOR_V1 if supported_build(expected_version) else expected_version


def profile_binding(origin: str, namespace: str, expected_version: str) -> str:
    # Local import keeps HTTP's safe diagnostic projection independent of this
    # policy. Preserve DEFAULT spaces/ensure_ascii/UTF-8, exactly as old receipts.
    from .metadata_http import _origin
    return hashlib.sha256(json.dumps([
        _origin(origin), namespace, binding_component(expected_version)
    ]).encode('utf-8')).hexdigest()
