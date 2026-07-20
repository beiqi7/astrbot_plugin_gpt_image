"""Security helpers: URL/IP/path validation, size limits, log redaction.

All network downloads, local file reads, and Base64 decodes should
route through this module so security policy is centralized.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import re
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from astrbot.api import logger

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:
    def get_astrbot_data_path() -> str:
        return str(Path.cwd() / "data")


DEFAULT_MAX_SINGLE_IMAGE_BYTES = 15 * 1024 * 1024
DEFAULT_MAX_TOTAL_REF_BYTES = 30 * 1024 * 1024
DEFAULT_MAX_OUTPUT_BYTES = 30 * 1024 * 1024
DEFAULT_MAX_IMAGE_PIXELS = 40_000_000
MAX_HTTP_CHUNK = 64 * 1024


class SecurityError(Exception):
    """Security validation failed. Log detail internally; show generic user text."""


def redact_url(url) -> str:
    if not url:
        return ""
    s = str(url)
    if s.startswith("data:"):
        return f"data:...({len(s)}B)"
    if s.startswith("base64://"):
        return f"base64:...({len(s)}B)"
    try:
        p = urlparse(s)
        netloc = p.netloc or ""
        digest = hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:8]
        return f"{p.scheme}://{netloc}/#{digest}"
    except Exception:
        return "url#" + hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:8]


def redact_path(p) -> str:
    s = str(p or "")
    if not s:
        return ""
    digest = hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:8]
    try:
        name = Path(s).name
    except Exception:
        name = ""
    return f"path:{name or '?'}#{digest}"


def redact_prompt(text: str) -> str:
    if not text:
        return ""
    digest = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:8]
    return f"prompt(len={len(text)},h={digest})"


_METADATA_HOSTS = {
    "169.254.169.254",
    "metadata.google.internal",
    "metadata.internal",
    "100.100.100.200",
    "fd00:ec2::254",
}


def _parse_allow_hosts(raw):
    out = []
    _SEP_RE = re.compile(r"[\s,;\uff0c\uff1b]+")
    for token in _SEP_RE.split(raw or ""):
        token = token.strip().lower()
        if not token:
            continue
        host = token
        port = None
        if ":" in token and not token.startswith("["):
            h, _, port_s = token.rpartition(":")
            if port_s.isdigit():
                host, port = h.strip(), int(port_s)
        out.append((host, port))
    return out


def _parse_allow_suffixes(raw):
    out = []
    _SEP_RE = re.compile(r"[\s,;\uff0c\uff1b]+")
    for token in _SEP_RE.split(raw or ""):
        token = token.strip().lower().lstrip(".")
        if token:
            out.append(token)
    return out


def _resolve_ips(host):
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return []
    out = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr or not sockaddr[0]:
            continue
        try:
            out.append(ipaddress.ip_address(sockaddr[0]))
        except ValueError:
            continue
    return out


def _ip_is_dangerous(ip) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


class UrlPolicy:
    """URL security policy.

    - allow_host_ports: whitelisted host[:port] pairs (e.g. NapCat loopback)
    - allow_domain_suffixes: whitelisted public domain suffixes (e.g. qpic.cn)
    - Blocks metadata addresses regardless of whitelist
    - Blocks private/loopback/link-local/multicast/reserved when host is not
      explicitly whitelisted
    - Requires HTTPS for other public hosts by default
    """

    def __init__(
        self,
        *,
        allow_host_ports=(),
        allow_domain_suffixes=(),
        require_https_for_public: bool = True,
        allow_public_http: bool = False,
    ):
        self.allow_host_ports = list(allow_host_ports or ())
        self.allow_domain_suffixes = [s.lower() for s in (allow_domain_suffixes or ())]
        self.require_https_for_public = require_https_for_public
        self.allow_public_http = allow_public_http

    @classmethod
    def from_config(
        cls,
        *,
        napcat_hosts: str = "",
        image_host_suffixes: str = "",
        allow_public_http: bool = False,
    ):
        return cls(
            allow_host_ports=_parse_allow_hosts(napcat_hosts),
            allow_domain_suffixes=_parse_allow_suffixes(image_host_suffixes),
            allow_public_http=bool(allow_public_http),
        )

    def _host_port_allowed(self, host, port):
        h = (host or "").lower()
        for allow_host, allow_port in self.allow_host_ports:
            if allow_host != h:
                continue
            if allow_port is None or port == allow_port:
                return True
        return False

    def _domain_suffix_allowed(self, host):
        h = (host or "").lower()
        if not h:
            return False
        for suf in self.allow_domain_suffixes:
            if h == suf or h.endswith("." + suf):
                return True
        return False

    def validate(self, url):
        """Return (allowed, reason). Reason empty when allowed."""
        if not url:
            return False, "empty_url"
        try:
            parsed = urlparse(url)
        except Exception:
            return False, "parse_error"

        scheme = (parsed.scheme or "").lower()
        if scheme not in ("http", "https"):
            return False, "scheme_denied:" + (scheme or "unknown")

        host = (parsed.hostname or "").lower()
        if not host:
            return False, "empty_host"

        port = parsed.port
        if port is None:
            port = 443 if scheme == "https" else 80

        if host in _METADATA_HOSTS:
            return False, "metadata_denied"

        if self._host_port_allowed(host, port):
            return True, ""

        ips = _resolve_ips(host)
        for ip in ips:
            if str(ip) in _METADATA_HOSTS:
                return False, "metadata_ip_denied"
            if _ip_is_dangerous(ip):
                return False, "private_ip_denied"

        if self._domain_suffix_allowed(host):
            return True, ""

        if scheme == "http" and not self.allow_public_http:
            if self.require_https_for_public:
                return False, "http_public_denied"

        return True, ""


def _resolve_safe(path):
    try:
        return Path(path).expanduser().resolve(strict=False)
    except Exception:
        return None


def _default_media_roots(extra=()):
    roots = []

    def _add(p):
        if p is None:
            return
        rp = _resolve_safe(p)
        if rp and rp not in roots:
            roots.append(rp)

    try:
        astrbot_data = _resolve_safe(get_astrbot_data_path())
        if astrbot_data:
            _add(astrbot_data)
            _add(astrbot_data / "temp")
            _add(astrbot_data / "cache")
            _add(astrbot_data / "plugin_data")
    except Exception:
        pass

    _add(tempfile.gettempdir())

    home = Path.home()
    for rel in (
        ".config/QQ/NapCat/temp",
        "AppData/Roaming/BetterUniverse/QQNT/NapCat/temp",
        "AppData/Local/Temp",
    ):
        _add(home / rel)

    for e in extra or ():
        _add(e)

    return roots


class PathPolicy:
    """Whitelist of local media directories that may be read."""

    def __init__(self, extra_roots=()):
        self.roots = _default_media_roots(extra_roots)

    @classmethod
    def from_config(cls, allowed_media_dirs: str = ""):
        extras = [
            p.strip()
            for p in re.split(r"[\r\n;\uff1b]+", allowed_media_dirs or "")
            if p.strip()
        ]
        return cls(extras)

    def is_allowed(self, path) -> bool:
        rp = _resolve_safe(path)
        if rp is None:
            return False
        try:
            if not rp.is_file():
                return False
        except OSError:
            return False
        for root in self.roots:
            try:
                rp.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def read_bytes(self, path, *, max_bytes: int) -> bytes:
        rp = _resolve_safe(path)
        if rp is None or not self.is_allowed(rp):
            raise SecurityError("path_not_allowed")
        try:
            size = rp.stat().st_size
        except OSError as e:
            raise SecurityError("stat_failed:" + str(e)) from e
        if size > max_bytes:
            raise SecurityError("file_too_large:" + str(size))
        with rp.open("rb") as f:
            data = f.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise SecurityError("file_too_large:" + str(len(data)))
        return data


def estimate_base64_bytes(payload: str) -> int:
    if not payload:
        return 0
    if ";base64," in payload:
        payload = payload.split(";base64,", 1)[1]
    n = len(payload)
    return (n // 4) * 3 + 3


def safe_b64decode(payload: str, *, max_bytes: int) -> bytes:
    if not payload:
        raise SecurityError("empty_base64")
    est = estimate_base64_bytes(payload)
    if est > max_bytes:
        raise SecurityError("base64_too_large:" + str(est))
    if ";base64," in payload:
        payload = payload.split(";base64,", 1)[1]
    payload = re.sub(r"\s+", "", payload)
    pad = (-len(payload)) % 4
    try:
        data = base64.b64decode(payload + "=" * pad, validate=False)
    except Exception as e:
        raise SecurityError("b64_decode_failed:" + str(e)) from e
    if len(data) > max_bytes:
        raise SecurityError("base64_too_large:" + str(len(data)))
    return data


_IMG_PNG = b"\x89PNG\r\n\x1a\n"


def sniff_image_mime(data: bytes) -> str:
    if not data or len(data) < 8:
        return ""
    if data.startswith(_IMG_PNG):
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def is_image_bytes(data: bytes) -> bool:
    return bool(sniff_image_mime(data))
