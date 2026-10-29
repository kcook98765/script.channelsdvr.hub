"""Basic image-reference handling only. Kodi owns network, decode and cache.

No endpoint discovery, image inspection, host trust policy or credential forwarding.
Relative paths resolve against configured origin + '/', preserving a path prefix;
root-relative paths follow normal urljoin semantics. Queries remain private.
"""
import re
from urllib.parse import unquote, urljoin, urlsplit


def _plain(value):
    if type(value) is not str or not value or len(value) > 2048:
        return False
    try:
        if len(value.encode('utf-8')) > 2048:
            return False
    except UnicodeEncodeError:
        return False
    # Reject encoded control/header/path escapes too; do not rewrite signed queries.
    for candidate in (value, unquote(value)):
        if any(ord(c) <= 32 or 127 <= ord(c) <= 159 or c in '\\|#' for c in candidate):
            return False
    return re.search(r'%(?![0-9a-fA-F]{2})', value) is None


def _http(value):
    if not _plain(value):
        return False
    try:
        parts = urlsplit(value)
        return (parts.scheme.lower() in ('http', 'https') and bool(parts.hostname)
            and parts.username is None and parts.password is None and not parts.fragment
            and not parts.netloc.endswith(':') and (parts.port is None or 1 <= parts.port <= 65535)
            and '%' not in parts.netloc and _authority(parts.netloc, parts.hostname))
    except ValueError:
        return False


def _authority(authority, hostname):
    """ASCII hostname/literal spelling only; no DNS or address trust decisions.

    DNS-style labels (including localhost, IPv4 spelling and ASCII punycode) or
    bracketed IPv6 only. No raw IDN conversion, zone IDs or IPvFuture support.
    """
    if not authority.isascii():
        return False
    if authority.startswith('['):
        # urlsplit already checks IPv6 syntax; also require exact bracket/port
        # grammar, rejecting suffixes it can otherwise leave after the bracket.
        return ':' in hostname and re.fullmatch(r'\[[0-9A-Fa-f:.]+\](?::[0-9]+)?', authority) is not None
    if re.fullmatch(r'[A-Za-z0-9.-]+(?::[0-9]+)?', authority) is None:
        return False
    host = hostname[:-1] if hostname.endswith('.') else hostname
    return len(host) <= 253 and all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                                   is not None for label in host.split('.'))


def resolve_reference(origin: str, reference: object) -> str | None:
    """Return a usable HTTP(S) reference, never a claim about image delivery/data."""
    if not _plain(reference) or not isinstance(reference, str) or reference.startswith('//'):
        return None
    try:
        parts = urlsplit(reference)
        if parts.scheme:
            return reference if _http(reference) else None
        if parts.netloc or not _http(origin):
            return None
        base = urlsplit(origin)
        if base.query:
            return None
        resolved = urljoin(origin.rstrip('/') + '/', reference)
        return resolved if _http(resolved) else None
    except ValueError:
        return None


def first_reference(origin: str, candidates: tuple[str, ...]) -> str | None:
    """Fallback is only for missing/invalid references, never asynchronous decode."""
    for candidate in candidates:
        resolved = resolve_reference(origin, candidate)
        if resolved is not None:
            return resolved
    return None
