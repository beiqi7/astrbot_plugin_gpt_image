"""adobe2api HTTP 客户端：调用 OpenAI 兼容接口生图。"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from astrbot.api import logger

from .security import (
    DEFAULT_MAX_OUTPUT_BYTES,
    MAX_HTTP_CHUNK,
    MAX_RESPONSE_BYTES,
    SecurityError,
    UrlPolicy,
    ValidatingResolver,
    check_image_pixel_limit,
    is_animated_image,
    is_image_bytes,
    redact_url,
    safe_b64decode,
    sniff_image_mime,
)


class Adobe2APIError(Exception):
    def __init__(
        self,
        message: str,
        status: int | None = None,
        body: str = "",
        *,
        retryable: bool = False,
        user_message: str | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.body = body
        self.retryable = retryable
        self.user_message = user_message or "生图失败，请稍后再试。"


def classify_upstream_error(status: int | None, body: str = "") -> tuple[bool, str]:
    """
    返回 (是否可重试, 对用户友好的简短文案)。
    不向用户暴露上游原始 JSON / 审核细节。
    """
    text = (body or "").lower()
    status = int(status or 0)

    if (
        "timeout_error" in text
        or "system under load" in text
        or "under load" in text
        or "overloaded" in text
        or status == 408
    ):
        return True, "服务繁忙或响应超时，请稍后再试。"

    if status == 429 or "rate limit" in text or "too many" in text:
        return True, "请求过于频繁，请稍后再试。"

    if status in (500, 502, 503, 504, 524):
        return True, "上游服务暂时不可用，请稍后再试。"

    if status in (401, 403):
        return False, "服务鉴权失败，请联系管理员检查配置。"

    if status == 400:
        return False, "请求参数不被接受，请换一种描述再试。"

    return False, "生图失败，请稍后再试。"


class Adobe2APIClient:
    """调用 leik1000/adobe2api 的 OpenAI 兼容接口。"""

    def __init__(
        self,
        base_url: str,
        api_key: str = "",
        timeout: float = 300.0,
        max_retries: int = 2,
        retry_backoff: float = 3.0,
        url_policy: UrlPolicy | None = None,
    ):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.timeout = float(timeout or 300)
        self.max_retries = max(0, int(max_retries or 0))
        self.retry_backoff = max(0.5, float(retry_backoff or 3.0))
        self._session: aiohttp.ClientSession | None = None
        self._url_policy = url_policy
        self._output_url_policy = url_policy
        self._max_output_bytes = DEFAULT_MAX_OUTPUT_BYTES
        self._allow_insecure_http = False

    def set_allow_insecure_http(self, val: bool) -> None:
        self._allow_insecure_http = bool(val)

    def set_max_output_bytes(self, n: int) -> None:
        self._max_output_bytes = max(1024, int(n))

    def set_output_url_policy(self, policy: "UrlPolicy | None") -> None:
        """Set stricter policy for output image downloads (no loopback)."""
        self._output_url_policy = policy

    def configured(self) -> bool:
        if not self.base_url:
            return False
        parsed = urlparse(self.base_url)
        if parsed.scheme not in ("http", "https"):
            return False
        if not parsed.hostname:
            return False
        # Reject URLs that carry credentials or extra components: they
        # have no legitimate use for an API endpoint and would silently
        # leak into logs / error messages.
        if parsed.username or parsed.password:
            logger.error(
                f"[gpt_image] base_url {redact_url(self.base_url)} contains "
                "userinfo credentials; refusing to start. Move the API key "
                "to the api_key field."
            )
            return False
        if parsed.query or parsed.fragment:
            logger.error(
                f"[gpt_image] base_url {redact_url(self.base_url)} must not "
                "contain query or fragment; refusing to start."
            )
            return False
        if parsed.scheme == "http":
            host = parsed.hostname.lower()
            is_loopback = host in ("127.0.0.1", "localhost", "::1")
            if not is_loopback and not self._allow_insecure_http:
                logger.error(
                    f"[gpt_image] base_url {redact_url(self.base_url)} uses "
                    f"HTTP to non-loopback host: blocked "
                    f"(set allow_insecure_api_http=true to override)"
                )
                return False
            if not is_loopback and self._allow_insecure_http:
                logger.warning(
                    f"[gpt_image] base_url {redact_url(self.base_url)} uses "
                    f"insecure HTTP: API key will be sent in cleartext"
                )
        return True

    def set_url_policy(self, policy: UrlPolicy | None) -> None:
        self._url_policy = policy

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
            headers["X-API-Key"] = self.api_key
        return headers

    def _url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return f"{self.base_url}{path}"

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=60, connect=30)
            if self._url_policy is not None:
                # Fail-closed: if resolver init fails, refuse to create session
                resolver = ValidatingResolver(self._url_policy)
                connector = aiohttp.TCPConnector(resolver=resolver, limit=0)
                self._session = aiohttp.ClientSession(
                    timeout=timeout, connector=connector
                )
            else:
                self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    @staticmethod
    async def _read_text_limited(
        resp: aiohttp.ClientResponse, max_bytes: int = MAX_RESPONSE_BYTES
    ) -> str:
        """Read response body as text with size limit (streaming)."""
        declared = resp.headers.get("Content-Length", "")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise Adobe2APIError(
                f"response too large: {declared} > {max_bytes}",
                user_message="服务返回数据过大。",
            )
        buf = bytearray()
        async for chunk in resp.content.iter_chunked(MAX_HTTP_CHUNK):
            buf.extend(chunk)
            if len(buf) > max_bytes:
                raise Adobe2APIError(
                    f"response stream exceeded {max_bytes}B",
                    user_message="服务返回数据过大。",
                )
        try:
            return buf.decode("utf-8", errors="replace")
        except Exception:
            return buf.decode("latin-1", errors="replace")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def generate_image(
        self,
        *,
        prompt: str,
        model: str,
        image_data_urls: list[str] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """
        调用 /v1/images/generations 或带参考图时走 /v1/chat/completions。

        返回:
          {
            "url": str | None,
            "b64": str | None,
            "model": str,
            "raw": dict,
          }
        """
        if not self.configured():
            raise Adobe2APIError("未配置 adobe2api base_url")

        last_err: Adobe2APIError | None = None
        attempts = 1 + self.max_retries

        for attempt in range(1, attempts + 1):
            try:
                return await self._do_generate(
                    prompt=prompt,
                    model=model,
                    image_data_urls=image_data_urls,
                    timeout=timeout,
                )
            except Adobe2APIError as e:
                last_err = e
                if not e.retryable or attempt >= attempts:
                    raise
                delay = self.retry_backoff * attempt
                logger.warning(
                    f"retryable failure ({attempt}/{attempts}), "
                    f"retry in {delay:.1f}s status={e.status}"
                )
                await asyncio.sleep(delay)

        if last_err:
            raise last_err
        raise Adobe2APIError(
            "生图失败",
            user_message="生图失败，请稍后再试。",
        )

    async def _do_generate(
        self,
        *,
        prompt: str,
        model: str,
        image_data_urls: list[str] | None,
        timeout: float | None,
    ) -> dict[str, Any]:
        session = await self._get_session()
        headers = self._auth_headers()
        req_timeout = aiohttp.ClientTimeout(total=float(timeout or self.timeout))

        if image_data_urls:
            # 图生图：必须走 chat.completions，且参考图优先 data URL
            content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for url in image_data_urls:
                u = str(url or "").strip()
                if not u:
                    continue
                content.append({"type": "image_url", "image_url": {"url": u}})
            if len(content) < 2:
                raise Adobe2APIError(
                    "图生图缺少有效参考图",
                    user_message="参考图无效，请重新发送图片后再试。",
                )
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": content}],
                "stream": False,
            }
            endpoint = self._url("/v1/chat/completions")
            logger.info(
                f"图生图请求 model={model} refs={len(content)-1} "
                f"prompt_len={len(prompt)} endpoint=/v1/chat/completions"
            )
        else:
            payload = {"model": model, "prompt": prompt}
            endpoint = self._url("/v1/images/generations")
            logger.info(
                f"文生图请求 model={model} prompt_len={len(prompt)} "
                f"endpoint=/v1/images/generations"
            )

        try:
            async with session.post(
                endpoint, json=payload, headers=headers, timeout=req_timeout,
                allow_redirects=False,
            ) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    raise Adobe2APIError(
                        f"API endpoint returned redirect {resp.status}, "
                        f"expected direct response",
                        user_message="服务配置异常，请联系管理员。",
                    )
                # Dynamic response limit: base64 expands ~33%, add JSON overhead
                resp_limit = max(
                    MAX_RESPONSE_BYTES,
                    int(self._max_output_bytes * 4 / 3) + 1024 * 1024,
                )
                text = await self._read_text_limited(resp, max_bytes=resp_limit)
                if resp.status >= 400:
                    retryable, user_msg = classify_upstream_error(resp.status, text)
                    import hashlib as _hl

                    body_hash = _hl.sha256(
                        text.encode("utf-8", "ignore")
                    ).hexdigest()[:12]
                    logger.error(
                        f"[gpt_image] generate failed HTTP {resp.status} "
                        f"len={len(text)} hash={body_hash} "
                        f"retryable={retryable}"
                    )
                    raise Adobe2APIError(
                        f"generate failed HTTP {resp.status} "
                        f"(len={len(text)} hash={body_hash})",
                        status=resp.status,
                        body=text,
                        retryable=retryable,
                        user_message=user_msg,
                    )
                # Parse JSON from already-read text (resp body is consumed
                # by _read_text_limited, so resp.json() would return empty)
                import json

                try:
                    data = json.loads(text)
                except Exception:
                    data = None
                if not isinstance(data, dict):
                    import hashlib as _hl

                    digest = _hl.sha256(text.encode("utf-8", "ignore")).hexdigest()[:12]
                    logger.warning(
                        f"[gpt_image] upstream non-JSON response "
                        f"len={len(text)} hash={digest}"
                    )
                    raise Adobe2APIError(
                        "upstream returned non-JSON or empty response",
                        status=resp.status,
                        user_message="上游返回异常，请稍后再试。",
                    )
        except Adobe2APIError:
            raise
        except asyncio.TimeoutError as e:
            raise Adobe2APIError(
                f"生图超时（{timeout or self.timeout}s）",
                status=408,
                retryable=True,
                user_message="服务繁忙或响应超时，请稍后再试。",
            ) from e
        except aiohttp.ClientError as e:
            raise Adobe2APIError(
                f"网络错误: {e}",
                retryable=True,
                user_message="网络异常，请稍后再试。",
            ) from e

        return self._extract_image_result(data, model)

    def _extract_image_result(self, data: dict[str, Any], model: str) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise Adobe2APIError(
                "upstream response is not a JSON object",
                user_message="上游返回异常，请稍后再试。",
            )
        url: str | None = None
        b64: str | None = None

        # OpenAI images.generations 格式
        items = data.get("data")
        if isinstance(items, list) and items:
            first = items[0] if isinstance(items[0], dict) else {}
            url = first.get("url") or first.get("image_url")
            b64 = first.get("b64_json") or first.get("b64")

        # chat.completions 格式
        if not url and not b64:
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                msg = choices[0].get("message") if isinstance(choices[0], dict) else {}
                content = (msg or {}).get("content") or ""
                if isinstance(content, list):
                    # multimodal content parts
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") == "image_url":
                            image_url = part.get("image_url") or {}
                            if isinstance(image_url, dict):
                                url = image_url.get("url") or url
                            elif isinstance(image_url, str):
                                url = image_url
                        if part.get("type") == "image" and part.get("data"):
                            b64 = part.get("data")
                    content = ""
                if isinstance(content, str) and content:
                    url, b64 = self._parse_content_for_image(content)

        if url and isinstance(url, str) and url.startswith("/"):
            url = urljoin(self.base_url + "/", url.lstrip("/"))

        if not url and not b64:
            import hashlib as _hl

            body_hash = _hl.sha256(
                str(data).encode("utf-8", "ignore")
            ).hexdigest()[:12]
            raise Adobe2APIError(
                f"响应中未找到图片 (hash={body_hash})",
                body=str(data)[:2000],
                user_message="上游未返回图片，请稍后再试。",
            )

        return {"url": url, "b64": b64, "model": data.get("model") or model, "raw": data}

    @staticmethod
    def _parse_content_for_image(content: str) -> tuple[str | None, str | None]:
        # markdown image
        m = re.search(r"!\[[^\]]*\]\((https?://[^\s)]+|data:image/[^)]+)\)", content)
        if m:
            val = m.group(1)
            if val.startswith("data:image"):
                return None, Adobe2APIClient._data_url_to_b64(val)
            return val, None

        m = re.search(r"(data:image/[a-zA-Z0-9.+-]+;base64,[A-Za-z0-9+/=]+)", content)
        if m:
            return None, Adobe2APIClient._data_url_to_b64(m.group(1))

        m = re.search(r"(https?://[^\s)\]\"'<>]+)", content)
        if m:
            return m.group(1), None

        return None, None

    @staticmethod
    def _data_url_to_b64(data_url: str) -> str:
        if "," in data_url:
            return data_url.split(",", 1)[1]
        return data_url

    def _download_headers(self, url: str) -> dict[str, str]:
        """Compute auth headers for a download URL.

        Only sends Authorization when the URL is strictly same-origin
        (scheme + host + port) as base_url. Prevents API key leakage
        via cross-origin redirects. Normalizes default ports.
        """
        try:
            target = urlparse(url)
            base = urlparse(self.base_url)
            tp = target.port or (443 if target.scheme == "https" else 80)
            bp = base.port or (443 if base.scheme == "https" else 80)
            if (
                target.scheme == base.scheme
                and target.hostname == base.hostname
                and tp == bp
                and self.api_key
            ):
                return {
                    "Authorization": f"Bearer {self.api_key}",
                    "X-API-Key": self.api_key,
                }
        except Exception:
            pass
        return {}

    async def download_bytes(
        self,
        url: str,
        *,
        timeout: float = 60.0,
        max_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> bytes:
        """Download bytes with SSRF validation, manual redirects, and size limit."""
        if self._output_url_policy is not None:
            ok, reason = await self._output_url_policy.validate_async(url)
            if not ok:
                raise Adobe2APIError(
                    f"output URL rejected: {reason}",
                    user_message="生成结果地址不安全，已拒绝下载。",
                )

        session = await self._get_session()
        req_timeout = aiohttp.ClientTimeout(total=max(5.0, float(timeout)), connect=15)
        current = url
        max_redirects = 5
        for _ in range(max_redirects + 1):
            if self._output_url_policy is not None:
                ok, reason = await self._output_url_policy.validate_async(current)
                if not ok:
                    raise Adobe2APIError(
                        f"redirect URL rejected: {reason}",
                        user_message="生成结果地址不安全，已拒绝下载。",
                    )
            # Recompute auth headers per-hop to prevent cross-origin key leakage
            hop_headers = self._download_headers(current)
            async with session.get(
                current, headers=hop_headers, timeout=req_timeout, allow_redirects=False
            ) as resp:
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location", "")
                    if not loc:
                        raise Adobe2APIError("redirect without Location")
                    current = urljoin(current, loc)
                    continue
                if resp.status >= 400:
                    text = await self._read_text_limited(resp, max_bytes=65536)
                    import hashlib as _hl

                    body_hash = _hl.sha256(
                        text.encode("utf-8", "ignore")
                    ).hexdigest()[:12]
                    raise Adobe2APIError(
                        f"download failed HTTP {resp.status} "
                        f"(len={len(text)} hash={body_hash})",
                        status=resp.status,
                    )
                declared = resp.headers.get("Content-Length", "")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise Adobe2APIError(
                        f"result image too large: {declared} > {max_bytes}",
                        user_message="生成结果过大，请联系管理员检查。",
                    )
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(MAX_HTTP_CHUNK):
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        raise Adobe2APIError(
                            f"result image stream exceeded {max_bytes}B",
                            user_message="生成结果过大，请联系管理员检查。",
                        )
                return bytes(buf)
        raise Adobe2APIError("too many redirects")

    async def save_result_image(
        self,
        result: dict[str, Any],
        dest_dir: Path,
        filename_stem: str,
        *,
        max_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
        max_pixels: int = 40_000_000,
    ) -> Path:
        """Save the result image to dest_dir with size + signature + pixel limits."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        b64 = result.get("b64")
        url = result.get("url")

        def _validate(raw: bytes) -> bytes:
            if not is_image_bytes(raw):
                raise Adobe2APIError(
                    "result is not a valid image",
                    user_message="生成结果不是有效图片。",
                )
            if not check_image_pixel_limit(raw, max_pixels):
                raise Adobe2APIError(
                    f"result pixels exceed limit {max_pixels}",
                    user_message="生成结果图片过大，请联系管理员检查。",
                )
            if is_animated_image(raw):
                raise Adobe2APIError(
                    "result is an animated image (GIF/APNG/animated WebP)",
                    user_message="生成结果为动画图片，已被拒绝。",
                )
            return raw

        def _ext(raw: bytes, fallback_url: str = "") -> str:
            mime = sniff_image_mime(raw)
            ext_map = {
                "image/jpeg": ".jpg",
                "image/webp": ".webp",
                "image/gif": ".gif",
                "image/png": ".png",
            }
            if mime in ext_map:
                return ext_map[mime]
            lower = str(fallback_url or "").lower()
            if ".jpg" in lower or ".jpeg" in lower:
                return ".jpg"
            if ".webp" in lower:
                return ".webp"
            if ".gif" in lower:
                return ".gif"
            return ".png"

        if b64:
            try:
                raw = safe_b64decode(str(b64), max_bytes=max_bytes)
            except SecurityError as e:
                raise Adobe2APIError(
                    f"result b64 too large or invalid: {e}",
                    user_message="生成结果过大，请联系管理员检查。",
                ) from e
            _validate(raw)
            path = dest_dir / f"{filename_stem}{_ext(raw)}"
            path.write_bytes(raw)
            return path

        if url and str(url).startswith("data:image"):
            try:
                raw_b64 = self._data_url_to_b64(str(url))
                raw = safe_b64decode(raw_b64, max_bytes=max_bytes)
            except SecurityError as e:
                raise Adobe2APIError(
                    f"result data URL too large: {e}",
                    user_message="生成结果过大，请联系管理员检查。",
                ) from e
            _validate(raw)
            path = dest_dir / f"{filename_stem}{_ext(raw)}"
            path.write_bytes(raw)
            return path

        if url:
            content = await self.download_bytes(
                str(url), max_bytes=max_bytes
            )
            _validate(content)
            path = dest_dir / f"{filename_stem}{_ext(content, str(url))}"
            path.write_bytes(content)
            return path

        raise Adobe2APIError("no image data to save")
