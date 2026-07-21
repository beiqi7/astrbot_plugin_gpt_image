"""Security helpers: URL/IP/path validation, size limits, log redaction.

All network downloads, local file reads, and Base64 decodes should
route through this module so security policy is centralized.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import os
import re
import socket
import time
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
MAX_RESPONSE_BYTES = 4 * 1024 * 1024


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
        # Never include userinfo (user:password@) in logs - it would
        # leak credentials that may be embedded in attacker URLs or
        # misconfigured base_urls.
        host = p.hostname or ""
        port = p.port
        authority = f"{host}:{port}" if port else host
        digest = hashlib.sha1(s.encode("utf-8", "ignore")).hexdigest()[:8]
        return f"{p.scheme}://{authority}/#{digest}"
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

        # Bare IP (IPv4 or IPv6) without port
        try:
            ipaddress.ip_address(token)
            out.append((token, None))
            continue
        except ValueError:
            pass

        # [IPv6]:port or [IPv6]
        if token.startswith("["):
            m = re.fullmatch(r"\[([^\]]+)\](?::(\d+))?", token)
            if m:
                host = m.group(1)
                port = int(m.group(2)) if m.group(2) else None
                out.append((host, port))
            continue

        # hostname:port or IPv4:port
        host = token
        port = None
        if ":" in token:
            candidate_host, _, port_s = token.rpartition(":")
            if port_s.isdigit():
                host = candidate_host.strip()
                port = int(port_s)
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


_DNS_CACHE: dict[str, tuple[float, list[ipaddress._BaseAddress]]] = {}
_DNS_CACHE_TTL = 60.0
_DNS_CACHE_MAX = 256
_DNS_TIMEOUT = 3.0


def _resolve_ips_sync(host: str) -> list:
    """Blocking DNS resolution. Callers on the event loop should use
    resolve_ips_async instead."""
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


# Backward compat alias
_resolve_ips = _resolve_ips_sync


def _dns_cache_get(host: str) -> list | None:
    entry = _DNS_CACHE.get(host)
    if not entry:
        return None
    ts, ips = entry
    if time.time() - ts > _DNS_CACHE_TTL:
        _DNS_CACHE.pop(host, None)
        return None
    return ips


def _dns_cache_put(host: str, ips: list) -> None:
    if len(_DNS_CACHE) >= _DNS_CACHE_MAX:
        # naive eviction: drop an arbitrary entry
        try:
            _DNS_CACHE.pop(next(iter(_DNS_CACHE)))
        except StopIteration:
            pass
    _DNS_CACHE[host] = (time.time(), list(ips))


async def resolve_ips_async(host: str, *, timeout: float = _DNS_TIMEOUT) -> list:
    """Async DNS resolution with cache + timeout. Never blocks event loop."""
    # Literal IP: no DNS needed
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    cached = _dns_cache_get(host)
    if cached is not None:
        return list(cached)
    try:
        ips = await asyncio.wait_for(
            asyncio.to_thread(_resolve_ips_sync, host), timeout=timeout
        )
    except asyncio.TimeoutError:
        logger.warning(f"[gpt_image] DNS timeout for {host}")
        return []
    except Exception as e:
        logger.warning(f"[gpt_image] DNS error for {host}: {e}")
        return []
    _dns_cache_put(host, ips)
    return ips


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

    strict_public_only: if True, ignores allow_host_ports (no loopback/private
    access even for whitelisted hosts). Used for URLs that came from
    untrusted sources (e.g. user Image.url field).
    """

    def __init__(
        self,
        *,
        allow_host_ports=(),
        allow_domain_suffixes=(),
        require_https_for_public: bool = True,
        allow_public_http: bool = False,
        strict_public_only: bool = False,
        allow_other_public_https: bool = True,
    ):
        self.allow_host_ports = list(allow_host_ports or ())
        self.allow_domain_suffixes = [s.lower() for s in (allow_domain_suffixes or ())]
        self.require_https_for_public = require_https_for_public
        self.allow_public_http = allow_public_http
        self.strict_public_only = bool(strict_public_only)
        # When False, only hosts whose domain suffix is whitelisted are
        # accepted (after IP checks). Used for untrusted user-provided
        # URLs to prevent the bot from being abused as an arbitrary
        # public HTTPS proxy. Default True for backward compatibility
        # (output URLs may legitimately live on arbitrary CDNs).
        self.allow_other_public_https = bool(allow_other_public_https)

    @classmethod
    def from_config(
        cls,
        *,
        napcat_hosts: str = "",
        image_host_suffixes: str = "",
        allow_public_http: bool = False,
        strict_public_only: bool = False,
        allow_other_public_https: bool = True,
    ):
        return cls(
            allow_host_ports=_parse_allow_hosts(napcat_hosts),
            allow_domain_suffixes=_parse_allow_suffixes(image_host_suffixes),
            allow_public_http=bool(allow_public_http),
            strict_public_only=bool(strict_public_only),
            allow_other_public_https=bool(allow_other_public_https),
        )

    def _host_port_allowed(self, host, port):
        if self.strict_public_only:
            return False
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

    def _validate_parsed(self, url: str, ips: list):
        """Shared post-parse validation given already-resolved IPs."""
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

        try:
            port = parsed.port
        except ValueError:
            return False, "invalid_port"
        if port is None:
            port = 443 if scheme == "https" else 80

        if host in _METADATA_HOSTS:
            return False, "metadata_denied"

        if self._host_port_allowed(host, port):
            return True, ""

        if not ips:
            return False, "dns_unresolved"
        for ip in ips:
            if str(ip) in _METADATA_HOSTS:
                return False, "metadata_ip_denied"
            if _ip_is_dangerous(ip):
                return False, "private_ip_denied"

        if scheme == "http" and not self.allow_public_http:
            if self.require_https_for_public:
                return False, "http_public_denied"

        if self._domain_suffix_allowed(host):
            return True, ""

        # Host is a public HTTPS host that didn't match any whitelist
        # suffix. Whether we accept it depends on the policy:
        # - allow_other_public_https=True (default): accept any public
        #   HTTPS. Required for output URLs that may live on arbitrary
        #   CDNs.
        # - allow_other_public_https=False: reject. Used for untrusted
        #   user-provided URLs so the bot cannot be abused as a public
        #   HTTPS proxy.
        if scheme == "https" and self.allow_other_public_https:
            return True, ""
        if scheme == "http" and self.allow_public_http:
            return True, ""
        return False, "host_not_whitelisted"

    def validate(self, url):
        """Return (allowed, reason). Reason empty when allowed.

        Uses blocking DNS. Prefer validate_async in event-loop contexts.
        DNS rebinding protection: use ValidatingResolver with aiohttp to
        re-check IPs at connection time, closing the TOCTOU gap between
        validate() and actual connection.
        """
        if not url:
            return False, "empty_url"
        try:
            parsed = urlparse(url)
        except Exception:
            return False, "parse_error"

        host = (parsed.hostname or "").lower()
        if not host:
            return False, "empty_host"

        try:
            port = parsed.port
        except ValueError:
            return False, "invalid_port"

        if self._host_port_allowed(host, port):
            return self._validate_parsed(url, [])

        ips = _resolve_ips_sync(host)
        return self._validate_parsed(url, ips)

    async def validate_async(self, url):
        """Async version of validate. Uses non-blocking DNS with cache+timeout."""
        if not url:
            return False, "empty_url"
        try:
            parsed = urlparse(url)
        except Exception:
            return False, "parse_error"

        host = (parsed.hostname or "").lower()
        if not host:
            return False, "empty_host"

        try:
            port = parsed.port
        except ValueError:
            return False, "invalid_port"

        if self._host_port_allowed(host, port):
            return self._validate_parsed(url, [])

        ips = await resolve_ips_async(host)
        return self._validate_parsed(url, ips)

    def filter_safe_ips(self, host: str, ips: list, *, port: int | None = None) -> list:
        """Filter out dangerous IPs unless host[:port] is whitelisted."""
        if self._host_port_allowed(host, port):
            return ips
        safe = []
        for ip in ips:
            if str(ip) in _METADATA_HOSTS:
                continue
            if _ip_is_dangerous(ip):
                continue
            safe.append(ip)
        return safe


class ValidatingResolver:
    """aiohttp-compatible resolver that blocks dangerous IPs at connect time.

    This closes the DNS rebinding TOCTOU gap: even if DNS changes between
    UrlPolicy.validate() and the actual connection, the resolver re-checks
    every IP before returning it to aiohttp.
    """

    def __init__(self, policy: UrlPolicy, *, inner=None):
        self._policy = policy
        if inner is not None:
            self._inner = inner
        else:
            from aiohttp import DefaultResolver

            self._inner = DefaultResolver()

    async def resolve(self, host: str, port: int = 0, family: int = 0):
        # Whitelisted hosts (e.g. 127.0.0.1:3000) bypass IP filtering.
        # Pass the actual port so host:port whitelist entries match.
        if self._policy._host_port_allowed(host, port):
            return await self._inner.resolve(host, port, family)

        results = await self._inner.resolve(host, port, family)

        safe_results = []
        for r in results:
            ip_str = r.get("host", "")
            try:
                ip = ipaddress.ip_address(ip_str)
            except ValueError:
                # Non-IP result: fail-closed, skip
                logger.warning(
                    f"[gpt_image] resolver: non-IP result "
                    f"'{ip_str}' for host {host}, skipping"
                )
                continue
            if str(ip) in _METADATA_HOSTS:
                logger.warning(
                    f"[gpt_image] resolver blocked metadata IP {ip_str} "
                    f"for host {host}"
                )
                continue
            if _ip_is_dangerous(ip):
                logger.warning(
                    f"[gpt_image] resolver blocked dangerous IP {ip_str} "
                    f"for host {host}"
                )
                continue
            safe_results.append(r)

        if not safe_results:
            raise OSError(
                f"all resolved IPs for {host} were blocked by security policy"
            )
        return safe_results

    async def close(self):
        if self._inner and hasattr(self._inner, "close"):
            await self._inner.close()


def _resolve_safe(path):
    try:
        return Path(path).expanduser().resolve(strict=False)
    except Exception:
        return None


def _default_media_roots(extra=()):
    """Default whitelisted media roots.

    Intentionally narrow: only the plugin's own data dir + system temp +
    known NapCat cache dirs. Does NOT include the whole AstrBot data root
    to reduce cross-plugin image exposure via signature-passing files.
    Callers can add more via `allowed_media_dirs` config.
    """
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
            # Only the plugin's own subtree + AstrBot temp/cache.
            _add(astrbot_data / "temp")
            _add(astrbot_data / "cache")
            _add(astrbot_data / "plugin_data" / "astrbot_plugin_gpt_image")
    except Exception:
        pass

    # NapCat / go-cqhttp / LLOneBot media cache dirs only
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

    # Extra roots that user config should never expand to (too broad).
    _FORBIDDEN_ROOTS = {"/", ""}

    def __init__(self, extra_roots=()):
        # Filter out obviously dangerous roots before merging.
        safe_extras = []
        for r in extra_roots or ():
            rp = _resolve_safe(r)
            if rp is None:
                continue
            s = str(rp)
            # reject filesystem root, user home root, and current working dir root
            if s in self._FORBIDDEN_ROOTS:
                logger.warning(
                    f"[gpt_image] path policy: ignoring forbidden root {s}"
                )
                continue
            # On Windows, rp.anchor is the drive root (e.g. "C:\\").
            # Path("C:\\") == "C:\\" is False (Path vs str), so compare
            # str(rp) to rp.anchor to detect drive roots correctly.
            if rp.anchor and str(rp) == rp.anchor:
                logger.warning(
                    f"[gpt_image] path policy: ignoring drive root {s}"
                )
                continue
            try:
                home = Path.home().resolve(strict=False)
                if rp == home:
                    logger.warning(
                        f"[gpt_image] path policy: ignoring user home root {s}"
                    )
                    continue
            except Exception:
                pass
            safe_extras.append(str(rp))
        self.roots = _default_media_roots(safe_extras)

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
    payload = payload.translate(str.maketrans("-_", "+/"))
    pad = (-len(payload)) % 4
    try:
        data = base64.b64decode(payload + "=" * pad, validate=True)
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


def probe_image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Return (width, height) from raw image bytes. None if unparseable."""
    if not data or len(data) < 12:
        return None

    if data.startswith(_IMG_PNG) and len(data) >= 24:
        try:
            w = int.from_bytes(data[16:20], "big")
            h = int.from_bytes(data[20:24], "big")
            if w > 0 and h > 0:
                return w, h
        except Exception:
            pass

    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        try:
            w = int.from_bytes(data[6:8], "little")
            h = int.from_bytes(data[8:10], "little")
            if w > 0 and h > 0:
                return w, h
        except Exception:
            pass

    if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 30:
        try:
            chunk = data[12:16]
            if chunk == b"VP8 " and len(data) >= 30:
                w = int.from_bytes(data[26:28], "little") & 0x3FFF
                h = int.from_bytes(data[28:30], "little") & 0x3FFF
                if w > 0 and h > 0:
                    return w, h
            elif chunk == b"VP8L" and len(data) >= 25:
                b0, b1, b2, b3 = data[21], data[22], data[23], data[24]
                w = 1 + (((b1 & 0x3F) << 8) | b0)
                h = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
                if w > 0 and h > 0:
                    return w, h
            elif chunk == b"VP8X" and len(data) >= 30:
                w = 1 + int.from_bytes(data[24:27], "little")
                h = 1 + int.from_bytes(data[27:30], "little")
                if w > 0 and h > 0:
                    return w, h
        except Exception:
            pass

    if data[:2] == b"\xff\xd8":
        try:
            i = 2
            n = len(data)
            while i + 9 < n:
                if data[i] != 0xFF:
                    return None
                while i < n and data[i] == 0xFF:
                    i += 1
                if i >= n:
                    return None
                marker = data[i]
                i += 1
                if (
                    (0xC0 <= marker <= 0xC3)
                    or (0xC5 <= marker <= 0xC7)
                    or (0xC9 <= marker <= 0xCB)
                    or (0xCD <= marker <= 0xCF)
                ):
                    if i + 7 > n:
                        return None
                    h = int.from_bytes(data[i + 3 : i + 5], "big")
                    w = int.from_bytes(data[i + 5 : i + 7], "big")
                    if w > 0 and h > 0:
                        return w, h
                    return None
                if marker in (0xD8, 0xD9):
                    continue
                if i + 2 > n:
                    return None
                seg_len = int.from_bytes(data[i : i + 2], "big")
                if seg_len < 2:
                    return None
                i += seg_len
            return None
        except Exception:
            return None

    return None


def check_image_pixel_limit(data: bytes, max_pixels: int) -> bool:
    """Return True if image dimensions are within limit.

    Returns False if:
    - dimensions exceed max_pixels
    - image signature matches but dimensions cannot be parsed (fail-closed)
    Returns True only if dimensions parse successfully and are within limit,
    or if the data is not a recognizable image (handled by is_image_bytes
    before this check).
    """
    dims = probe_image_dimensions(data)
    if dims is None:
        # Signature matched (caller checked is_image_bytes) but we can't
        # parse dimensions -> reject to prevent decompression bombs
        return False
    w, h = dims
    return w * h <= max_pixels


def is_animated_image(data: bytes) -> bool:
    """Detect animated GIF / APNG / animated WebP without decoding pixels.

    Returns True only for animated content. Static PNG/JPEG/WebP/GIF
    return False. Unknown formats return False (caller checks
    is_image_bytes separately).

    Animation is a decompression-bomb amplification vector: a small file
    can contain thousands of frames whose combined decoded pixels exceed
    memory even when width*height is modest.
    """
    if not data or len(data) < 12:
        return False

    # ---- GIF87a / GIF89a -----------------------------------------------
    # Multi-frame GIF: count Image Descriptor blocks (0x2C).
    # If there are 2+ frames, treat as animated.
    if data[:6] in (b"GIF87a", b"GIF89a"):
        try:
            frames = 0
            i = 13  # skip header (6) + logical screen descriptor (7)
            # skip global color table if present
            packed = data[10]
            if packed & 0x80:
                gct_size = 3 * (1 << ((packed & 0x07) + 1))
                i += gct_size
            n = len(data)
            while i < n and frames < 2:
                b = data[i]
                if b == 0x3B:  # trailer
                    break
                if b == 0x21:  # extension
                    i += 2
                    if i >= n:
                        break
                    # skip sub-blocks
                    while i < n:
                        sz = data[i]
                        i += 1
                        if sz == 0:
                            break
                        i += sz
                    continue
                if b == 0x2C:  # image descriptor
                    frames += 1
                    if frames >= 2:
                        return True
                    if i + 10 > n:
                        break
                    packed_img = data[i + 9]
                    i += 10
                    if packed_img & 0x80:
                        lct_size = 3 * (1 << ((packed_img & 0x07) + 1))
                        i += lct_size
                    # LZW min code size
                    if i >= n:
                        break
                    i += 1
                    # skip sub-blocks
                    while i < n:
                        sz = data[i]
                        i += 1
                        if sz == 0:
                            break
                        i += sz
                    continue
                # unknown byte, bail
                break
            return frames >= 2
        except Exception:
            # Fail-closed for GIF: unparseable structure -> treat as animated
            return True

    # ---- WebP: RIFF ... WEBP -------------------------------------------
    # Animated WebP has "ANIM" chunk (VP8X + ANIM/ANMF).
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        head = data[12:min(len(data), 4096)]
        # VP8X flag byte after chunk header
        if head[:4] == b"VP8X" and len(head) >= 12:
            try:
                flags = head[8]
                # bit 1 (0x02) = ANIM flag
                if flags & 0x02:
                    return True
            except Exception:
                return True
        # Also look for ANIM/ANMF chunk id directly in first 4KB
        if b"ANIM" in head or b"ANMF" in head:
            return True
        return False

    # ---- APNG: PNG with acTL chunk before IDAT -------------------------
    if data.startswith(_IMG_PNG):
        # Scan first 64KB for acTL
        head = data[:min(len(data), 65536)]
        return b"acTL" in head

    return False
