"""Extract reference images from an AstrMessageEvent.

Tuned for NapCat + AstrBot (aiocqhttp / OneBot v11).

Security (v1.5.5+):
  - SSRF: all HTTP downloads go through UrlPolicy
  - Size limits: single image capped at max_bytes, stream-read
  - Local path limits: only files inside PathPolicy roots may be read
  - Base64 payloads capped to prevent memory DoS
  - Log redaction: URLs/paths appear as short digests
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp

from astrbot.api import logger

from .security import (
    DEFAULT_MAX_SINGLE_IMAGE_BYTES,
    MAX_HTTP_CHUNK,
    PathPolicy,
    SecurityError,
    UrlPolicy,
    is_image_bytes,
    redact_path,
    redact_url,
    safe_b64decode,
    sniff_image_mime,
)

_NAPCAT_FILE_ID_RE = re.compile(
    r"^[A-Za-z0-9_\-]{6,}\.(jpg|jpeg|png|gif|webp|bmp)$",
    re.IGNORECASE,
)


def _is_image_like(comp: Any) -> bool:
    if comp is None:
        return False
    name = type(comp).__name__
    if name in {"Image", "AstrImage"}:
        return True
    t = getattr(comp, "type", None)
    t_name = getattr(t, "name", None) or getattr(t, "value", None) or str(t or "")
    return str(t_name).lower() in {"image", "img", "picture", "pic"}


def _is_reply_like(comp: Any) -> bool:
    name = type(comp).__name__
    if name == "Reply":
        return True
    t = getattr(comp, "type", None)
    t_name = getattr(t, "name", None) or getattr(t, "value", None) or str(t or "")
    return str(t_name).lower() == "reply"


def _bytes_to_data_url(data: bytes, mime: str | None = None) -> str:
    import base64 as _b64

    mime = mime or sniff_image_mime(data) or "image/png"
    return f"data:{mime};base64,{_b64.b64encode(data).decode('ascii')}"

def _decode_data_url_head(data_url: str, *, max_bytes: int = 4096) -> bytes | None:
    """Decode just the leading bytes of a data URL for header sniffing."""
    if not data_url or ";base64," not in data_url:
        return None
    try:
        import base64 as _b64

        payload = data_url.split(";base64,", 1)[1]
        needed_b64 = min(len(payload), ((max_bytes + 2) // 3) * 4 + 8)
        head = payload[:needed_b64]
        pad = (-len(head)) % 4
        return _b64.b64decode(head + "=" * pad, validate=False)
    except Exception:
        return None


def probe_image_size(data_url: str) -> tuple[int, int] | None:
    """Return (width, height) from a data URL by parsing header bytes only."""
    data = _decode_data_url_head(data_url, max_bytes=8192)
    if not data or len(data) < 12:
        return None

    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
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


def _pure_b64_to_data_url(raw: str, *, max_bytes: int) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith("data:image") and ";base64," in s:
        try:
            data = safe_b64decode(s, max_bytes=max_bytes)
        except SecurityError as e:
            logger.warning(f"[gpt_image] base64 rejected: {e}")
            return None
        except Exception:
            return None
        if is_image_bytes(data):
            return s
        return None
    if s.startswith("base64://"):
        s = s[len("base64://") :]
    if _NAPCAT_FILE_ID_RE.match(s):
        return None
    if len(s) < 64:
        return None
    s = re.sub(r"\s+", "", s)
    try:
        data = safe_b64decode(s, max_bytes=max_bytes)
    except SecurityError as e:
        logger.warning(f"[gpt_image] base64 rejected: {e}")
        return None
    except Exception:
        return None
    if not is_image_bytes(data):
        return None
    return _bytes_to_data_url(data)


def _is_napcat_file_id(token: str) -> bool:
    t = (token or "").strip()
    if not t or t.startswith(("http://", "https://", "file://", "base64://", "data:")):
        return False
    if _NAPCAT_FILE_ID_RE.match(t):
        return True
    if re.match(r"^[A-Fa-f0-9]{16,}$", t):
        return True
    return False


def _onebot_routing_params(event) -> dict:
    params: dict = {}
    try:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        self_id = None
        if isinstance(raw, dict):
            self_id = raw.get("self_id")
        elif raw is not None:
            self_id = getattr(raw, "self_id", None)
            if self_id is None and hasattr(raw, "get"):
                self_id = raw.get("self_id")
        if self_id is None and hasattr(event, "get_self_id"):
            self_id = event.get_self_id()
        if self_id is not None and str(self_id).strip():
            sid = str(self_id).strip()
            params["self_id"] = int(sid) if sid.isdigit() else sid
    except Exception:
        pass
    return params


async def _onebot_call_action(event, action: str, **kwargs) -> Any:
    bot = getattr(event, "bot", None)
    if bot is None:
        return None
    payload = {**kwargs, **_onebot_routing_params(event)}
    try:
        if hasattr(bot, "call_action"):
            return await bot.call_action(action, **payload)
        if hasattr(bot, "api") and hasattr(bot.api, "call_action"):
            return await bot.api.call_action(action, **payload)
    except Exception as e:
        logger.debug(f"OneBot/NapCat {action} failed: {e}")
    return None


async def _napcat_get_image(
    event,
    file_token: str,
    *,
    url_policy: UrlPolicy,
    path_policy: PathPolicy,
    max_bytes: int,
) -> str | None:
    """NapCat / go-cqhttp: get_image. Returns data URL or None."""
    if not file_token or not getattr(event, "bot", None):
        return None

    tokens: list[str] = []
    for t in (file_token,):
        t = str(t or "").strip()
        if not t:
            continue
        tokens.append(t)
        if t.startswith("file://"):
            tokens.append(t[7:])
        try:
            parsed = urlparse(t)
            if parsed.path:
                name = parsed.path.rsplit("/", 1)[-1]
                if name:
                    tokens.append(name)
            qs = parse_qs(parsed.query or "")
            for k in ("file", "file_id", "fileid"):
                if qs.get(k):
                    tokens.append(qs[k][0])
        except Exception:
            pass

    tried: set[str] = set()
    for token in tokens:
        if not token or token in tried:
            continue
        tried.add(token)
        for kwargs in ({"file": token}, {"file_id": token}):
            resp = await _onebot_call_action(event, "get_image", **kwargs)
            if not isinstance(resp, dict):
                continue
            logger.debug(f"NapCat get_image keys={list(resp.keys())}")

            for key in ("base64", "data"):
                if resp.get(key):
                    data_url = _pure_b64_to_data_url(
                        str(resp[key]), max_bytes=max_bytes
                    )
                    if data_url:
                        logger.info("NapCat get_image: base64 ok")
                        return data_url

            for key in ("file", "path", "file_path"):
                fpath = resp.get(key)
                if not fpath:
                    continue
                try:
                    raw = path_policy.read_bytes(str(fpath), max_bytes=max_bytes)
                except SecurityError as e:
                    logger.warning(
                        f"NapCat get_image path rejected: {e} "
                        f"path={redact_path(str(fpath))}"
                    )
                    continue
                except Exception:
                    continue
                if is_image_bytes(raw):
                    logger.info("NapCat get_image: local file ok")
                    return _bytes_to_data_url(raw)

            url = resp.get("url")
            if url and str(url).startswith("http"):
                got = await _http_download_image(
                    str(url),
                    url_policy=url_policy,
                    max_bytes=max_bytes,
                )
                if got:
                    logger.info("NapCat get_image: url download ok")
                    return got
    return None


async def _http_download_image(
    url: str,
    *,
    url_policy: UrlPolicy,
    max_bytes: int,
) -> str | None:
    """Download an image with SSRF + size limits, streaming chunked reads."""
    ok, reason = url_policy.validate(url)
    if not ok:
        logger.warning(
            f"[gpt_image] URL rejected: {reason} url={redact_url(url)}"
        )
        return None

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    try:
        host = (urlparse(url).netloc or "").lower()
        if host:
            headers["Referer"] = f"http://{host}/"
        if any(
            x in host
            for x in ("qpic.cn", "qq.com", "myqcloud.com", "gtimg.cn")
        ):
            headers["Referer"] = "https://qun.qq.com/"
            headers["Origin"] = "https://qun.qq.com"
    except Exception:
        pass

    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    try:
        async with aiohttp.ClientSession(
            headers=headers, trust_env=True, timeout=timeout
        ) as session:
            return await _http_download_with_redirects(
                url, session, url_policy=url_policy, max_bytes=max_bytes
            )
    except Exception as e:
        logger.warning(f"[gpt_image] download error: {e} url={redact_url(url)}")
        return None


async def _http_download_with_redirects(
    url: str,
    session: aiohttp.ClientSession,
    *,
    url_policy: UrlPolicy,
    max_bytes: int,
    max_redirects: int = 5,
) -> str | None:
    """Download with manual redirect handling; re-validate every hop."""
    current = url
    visited = 0
    while visited <= max_redirects:
        ok, reason = url_policy.validate(current)
        if not ok:
            logger.warning(
                f"[gpt_image] redirect URL rejected: {reason} "
                f"url={redact_url(current)}"
            )
            return None
        try:
            async with session.get(
                current, allow_redirects=False
            ) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location", "")
                    if not loc:
                        return None
                    visited += 1
                    try:
                        from urllib.parse import urljoin

                        current = urljoin(current, loc)
                    except Exception:
                        current = loc
                    continue
                if resp.status != 200:
                    logger.warning(
                        f"[gpt_image] download HTTP {resp.status} "
                        f"url={redact_url(current)}"
                    )
                    return None

                mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
                declared_len = resp.headers.get("Content-Length", "")
                if declared_len and declared_len.isdigit():
                    if int(declared_len) > max_bytes:
                        logger.warning(
                            f"[gpt_image] content-length {declared_len} "
                            f"> limit {max_bytes} url={redact_url(current)}"
                        )
                        return None

                buf = bytearray()
                async for chunk in resp.content.iter_chunked(MAX_HTTP_CHUNK):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        logger.warning(
                            f"[gpt_image] stream exceeded {max_bytes}B "
                            f"url={redact_url(current)}"
                        )
                        return None
                data = bytes(buf)
                if not is_image_bytes(data):
                    logger.warning(
                        f"[gpt_image] downloaded non-image "
                        f"url={redact_url(current)}"
                    )
                    return None
                if not mime.startswith("image/"):
                    mime = sniff_image_mime(data)
                return _bytes_to_data_url(data, mime)
        except aiohttp.ClientError as e:
            logger.warning(
                f"[gpt_image] download client error: {e} "
                f"url={redact_url(current)}"
            )
            return None
        except Exception as e:
            logger.warning(
                f"[gpt_image] download error: {e} url={redact_url(current)}"
            )
            return None
    logger.warning("[gpt_image] too many redirects")
    return None


async def image_component_to_data_url(
    comp: Any,
    event=None,
    *,
    url_policy: UrlPolicy | None = None,
    path_policy: PathPolicy | None = None,
    max_bytes: int = DEFAULT_MAX_SINGLE_IMAGE_BYTES,
) -> str | None:
    """Convert an Image component to a data URL.

    Security: all HTTP downloads go through url_policy; all local file
    reads go through path_policy. Falls back to permissive policies
    only when caller passes None (not recommended for untrusted input).
    """
    if url_policy is None:
        url_policy = UrlPolicy()
    if path_policy is None:
        path_policy = PathPolicy()

    file_field = str(getattr(comp, "file", None) or "").strip()
    url_field = str(getattr(comp, "url", None) or "").strip()
    path_field = str(getattr(comp, "path", None) or "").strip()

    logger.info(
        f"[gpt_image] parse image comp: file={redact_path(file_field)} "
        f"url={redact_url(url_field)} path={redact_path(path_field)} "
        f"has_bot={bool(getattr(event, 'bot', None))}"
    )

    # 1) AstrBot official: convert_to_base64
    if hasattr(comp, "convert_to_base64"):
        try:
            raw = await comp.convert_to_base64()
            data_url = _pure_b64_to_data_url(str(raw or ""), max_bytes=max_bytes)
            if data_url:
                logger.info("[gpt_image] ok: convert_to_base64")
                return data_url
            logger.warning("[gpt_image] convert_to_base64 unparseable, fallback")
        except Exception as e:
            logger.warning(f"[gpt_image] convert_to_base64 failed: {e}")

    # 2) convert_to_file_path
    if hasattr(comp, "convert_to_file_path"):
        try:
            fpath = await comp.convert_to_file_path()
            if fpath:
                try:
                    raw = path_policy.read_bytes(str(fpath), max_bytes=max_bytes)
                    if is_image_bytes(raw):
                        logger.info("[gpt_image] ok: convert_to_file_path")
                        return _bytes_to_data_url(raw)
                except SecurityError as e:
                    logger.warning(
                        f"[gpt_image] convert_to_file_path path rejected: {e} "
                        f"path={redact_path(str(fpath))}"
                    )
        except Exception as e:
            logger.warning(f"[gpt_image] convert_to_file_path failed: {e}")

    # 3) NapCat: file as cache name -> get_image
    if event is not None and getattr(event, "bot", None) is not None:
        for token in (file_field, url_field, path_field):
            if not token:
                continue
            if _is_napcat_file_id(token) or token.startswith("http"):
                got = await _napcat_get_image(
                    event,
                    token,
                    url_policy=url_policy,
                    path_policy=path_policy,
                    max_bytes=max_bytes,
                )
                if got:
                    return got
            elif not token.startswith(("data:", "base64://")):
                got = await _napcat_get_image(
                    event,
                    token,
                    url_policy=url_policy,
                    path_policy=path_policy,
                    max_bytes=max_bytes,
                )
                if got:
                    return got

    # 4) Already data/base64
    for token in (file_field, url_field, path_field):
        data_url = _pure_b64_to_data_url(token, max_bytes=max_bytes)
        if data_url:
            return data_url

    # 5) Local path (whitelisted roots only)
    for token in (path_field, file_field, url_field):
        if not token:
            continue
        local = token[7:] if token.startswith("file://") else token
        try:
            raw = path_policy.read_bytes(local, max_bytes=max_bytes)
        except SecurityError as e:
            logger.warning(
                f"[gpt_image] local path rejected: {e} "
                f"path={redact_path(local)}"
            )
            continue
        except Exception:
            continue
        if is_image_bytes(raw):
            return _bytes_to_data_url(raw)

    # 6) HTTP download (SSRF-checked, size-limited)
    for token in (url_field, file_field):
        if token.startswith("http://") or token.startswith("https://"):
            got = await _http_download_image(
                token,
                url_policy=url_policy,
                max_bytes=max_bytes,
            )
            if got:
                logger.info("[gpt_image] ok: HTTP download")
                return got

    logger.error(
        f"[gpt_image] all extraction methods failed "
        f"file={redact_path(file_field)} url={redact_url(url_field)} "
        f"bot={type(getattr(event, 'bot', None)).__name__}"
    )
    return None


def iter_message_components(event) -> list[Any]:
    """Current message + Reply.chain."""
    comps: list[Any] = []
    try:
        chain = list(event.get_messages() or [])
    except Exception:
        try:
            chain = list(
                getattr(getattr(event, "message_obj", None), "message", None) or []
            )
        except Exception:
            chain = []

    for c in chain:
        comps.append(c)
        if _is_reply_like(c):
            reply_chain = getattr(c, "chain", None) or []
            for rc in reply_chain:
                comps.append(rc)
    return comps


def count_image_like(event) -> int:
    return sum(1 for c in iter_message_components(event) if _is_image_like(c))


async def collect_reference_data_urls(
    event,
    *,
    max_images: int = 3,
    url_policy: UrlPolicy | None = None,
    path_policy: PathPolicy | None = None,
    max_single_bytes: int = DEFAULT_MAX_SINGLE_IMAGE_BYTES,
    max_total_bytes: int = 0,
) -> list[str]:
    """Extract reference images from event, return list of data URLs.

    Security:
      - url_policy: SSRF validation for all HTTP downloads
      - path_policy: whitelist for local file reads
      - max_single_bytes: per-image byte cap
      - max_total_bytes: total cap across all images (0 = use default)
    """
    max_images = max(1, min(int(max_images or 3), 8))
    if max_total_bytes <= 0:
        from .security import DEFAULT_MAX_TOTAL_REF_BYTES

        max_total_bytes = DEFAULT_MAX_TOTAL_REF_BYTES
    if url_policy is None:
        url_policy = UrlPolicy()
    if path_policy is None:
        path_policy = PathPolicy()

    results: list[str] = []
    seen: set[str] = set()
    total_bytes = 0

    comps = iter_message_components(event)
    platform = ""
    try:
        platform = str(
            getattr(event, "get_platform_name", lambda: "")()
            or getattr(getattr(event, "platform_meta", None), "name", "")
            or ""
        )
    except Exception:
        platform = ""

    logger.info(
        f"[gpt_image] collect refs platform={platform!r} "
        f"comps={len(comps)} "
        f"image_like={sum(1 for c in comps if _is_image_like(c))} "
        f"bot={type(getattr(event, 'bot', None)).__name__}"
    )

    for comp in comps:
        if not _is_image_like(comp):
            continue
        data_url = await image_component_to_data_url(
            comp,
            event=event,
            url_policy=url_policy,
            path_policy=path_policy,
            max_bytes=max_single_bytes,
        )
        if not data_url:
            continue
        # estimate decoded bytes from base64 length
        payload_len = len(data_url) - len(data_url.split(";base64,", 1)[0]) - 8
        est_bytes = max(0, (payload_len // 4) * 3)
        if total_bytes + est_bytes > max_total_bytes:
            logger.warning(
                f"[gpt_image] total ref bytes {total_bytes + est_bytes} "
                f"> limit {max_total_bytes}, stopping"
            )
            break
        total_bytes += est_bytes
        key = data_url[:180]
        if key in seen:
            continue
        seen.add(key)
        results.append(data_url)
        logger.info(
            f"[gpt_image] ref ok #{len(results)} est_bytes={est_bytes} "
            f"total={total_bytes}"
        )
        if len(results) >= max_images:
            break

    if not results:
        logger.warning("[gpt_image] no reference data URLs extracted")
    return results
