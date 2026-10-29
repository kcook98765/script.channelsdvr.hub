"""Presentation-only summaries. No field here grants action authority."""
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from .newbuild_compatibility import supported_build


def counter(value):
    return value if type(value) is int and 0 <= value <= 2**63-1 else None


def short_text(value):
    return value if type(value) is str and re.fullmatch(r'[A-Za-z0-9_. +()-]{1,64}', value) else None


def running_seconds(start, now):
    if type(start) is not str or len(start) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(start[:-1] + '+00:00' if start.endswith('Z') else start)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        seconds = now - parsed.astimezone(timezone.utc).timestamp()
        return int(seconds) if 0 <= seconds <= 2**53 else None
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def storage_percent(value):
    if type(value) is not dict:
        return None
    used, free, total = (counter(value.get(k)) for k in ('used', 'free', 'total'))
    if (None in (used, free, total) or total == 0 or used > total or free > total
            or used + free != total):
        return None
    return round(used * 100 / total)


def parse_resource(name, value, now):
    """Return only whitelisted scalars; never retain raw server objects/paths."""
    if name == 'server':
        if type(value) is not dict or short_text(value.get('version')) is None:
            raise ValueError('shape')
        return dict(version=short_text(value.get('version')), os=short_text(value.get('os')),
                    arch=short_text(value.get('arch')), running=running_seconds(value.get('start_time'), now))
    if name == 'dvr':
        if type(value) is not dict:
            raise ValueError('shape')
        stats = value.get('stats')
        if type(stats) is not dict:
            stats = {}
        return dict(((k, counter(stats.get(k))) for k in ('files', 'total', 'jobs', 'rules', 'groups')),
                    storage=storage_percent(value.get('disk')),
                    enabled=value.get('enabled') if type(value.get('enabled')) is bool else None,
                    busy=value.get('busy') if type(value.get('busy')) is bool else None)
    if name == 'sources':
        if type(value) is not list or len(value) > 4096:
            raise ValueError('shape')
        seen, tuners, reporting, unknown = set(), 0, 0, 0
        for row in value:
            if type(row) is not dict:
                raise ValueError('shape')
            identity = row.get('DeviceID')
            if type(identity) is not str or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', identity) or identity in seen:
                raise ValueError('shape')
            seen.add(identity)
            count = counter(row.get('TunerCount'))
            if count is None:
                unknown += 1
            else:
                tuners += count
                reporting += 1
        return dict(sources=len(seen), tuners=tuners, reporting=reporting, unknown=unknown)
    if name == 'passes':
        if type(value) is not list or len(value) > 20000 or any(type(r) is not dict for r in value):
            raise ValueError('shape')
        paused = sum(r.get('Paused') is True for r in value)
        active = sum(r.get('Paused') is False for r in value)
        return dict(total=len(value), paused=paused, active=active, unknown=len(value)-paused-active)
    if name == 'activity':
        if type(value) is not dict:
            raise ValueError('shape')
        # Only explicitly reported activity arrays, not capacity/free-tuner proof.
        return {k: len(value[k]) if type(value.get(k)) is list else None
                for k in ('streams', 'recordings', 'workers', 'other')}
    raise ValueError('resource')


@dataclass(frozen=True)
class ResourceSummary:
    namespace: str
    version: str
    horizon: tuple[int, int]
    fetched_at: int
    loaded: int
    counts: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class ResourceValue:
    values: tuple[tuple[str, object], ...]
    fetched_at: float
    unavailable: bool = False


class DashboardState:
    def __init__(self):
        self.values = {}
        self.reported_version = None
        self.accepted_version = None
        self.unsupported_version = None
        self.error = False

    def merge(self, result):
        self.reported_version = result.version
        self.error = result.error
        if result.unsupported and result.version is not None:
            self.values.clear()
            self.accepted_version = None
            self.unsupported_version = result.version
            return
        coherent = (result.coherent if result.coherent is not None else
                    any(v is not None for _, v in result.resources))
        if not coherent or not supported_build(result.version):
            # Failed/unknown/drifting observations never retag accepted values.
            self.values = {k: ResourceValue(v.values, v.fetched_at, True) for k, v in self.values.items()}
            return
        if result.version != self.accepted_version:
            self.values.clear()
        self.accepted_version = result.version
        self.unsupported_version = None
        for name, values in result.resources:
            old = self.values.get(name)
            if values is not None and old is not None:
                prior = dict(old.values)
                retained = any(v is None and prior.get(k) is not None for k, v in values)
                merged = tuple((k, prior[k] if v is None and prior.get(k) is not None else v) for k, v in values)
                # Card age conservatively describes its oldest retained value.
                self.values[name] = ResourceValue(merged, old.fetched_at if retained else result.fetched_at, retained)
            elif values is not None:
                self.values[name] = ResourceValue(values, result.fetched_at)
            elif old is not None:
                self.values[name] = ResourceValue(old.values, old.fetched_at, True)
