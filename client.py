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
    SecurityError,
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

    if status in (500, 502, 503, 504):
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
    ):
        self.base_url = (base_url or "").strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.timeout = float(timeout or 300)
        self.max_retries = max(0, int(max_retries or 0))
        self.retry_backoff = max(0.5, float(retry_backoff or 3.0))
        self._session: aiohttp.ClientSession | None = None

    def configured(self) -> bool:
        return bool(self.base_url)

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
            # session 默认短超时，长等待由单次请求 timeout 覆盖
            timeout = aiohttp.ClientTimeout(total=60, connect=30)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

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
            except Exception:
                raise

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
                endpoint, json=payload, headers=headers, timeout=req_timeout
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    retryable, user_msg = classify_upstream_error(resp.status, text)
                    raise Adobe2APIError(
                        f"生图失败 HTTP {resp.status}: {text[:500]}",
                        status=resp.status,
                        body=text,
                        retryable=retryable,
                        user_message=user_msg,
                    )
                try:
                    data = await resp.json(content_type=None)
                except Exception:
                    import json

                    data = json.loads(text)
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
            raise Adobe2APIError(
                f"响应中未找到图片: {str(data)[:400]}",
                body=str(data)[:2000],
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

    async def download_bytes(
        self,
        url: str,
        *,
        timeout: float = 60.0,
        max_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> bytes:
        """Download bytes with streaming + size limit.

        Raises SecurityError if the response exceeds max_bytes.
        """
        session = await self._get_session()
        headers = {}
        try:
            host = urlparse(url).netloc
            base_host = urlparse(self.base_url).netloc
            if host == base_host and self.api_key:
                headers["Authorization"] = f"Bearer {self.api_key}"
        except Exception:
            pass

        req_timeout = aiohttp.ClientTimeout(total=max(5.0, float(timeout)), connect=15)
        async with session.get(
            url, headers=headers, timeout=req_timeout, allow_redirects=True
        ) as resp:
            if resp.status >= 400:
                text = await resp.text()
                raise Adobe2APIError(
                    f"download failed HTTP {resp.status}: {text[:200]}",
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

    async def save_result_image(
        self,
        result: dict[str, Any],
        dest_dir: Path,
        filename_stem: str,
        *,
        max_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    ) -> Path:
        """Save the result image to dest_dir with size limit."""
        dest_dir.mkdir(parents=True, exist_ok=True)
        b64 = result.get("b64")
        url = result.get("url")

        if b64:
            try:
                raw = safe_b64decode(str(b64), max_bytes=max_bytes)
            except SecurityError as e:
                raise Adobe2APIError(
                    f"result b64 too large or invalid: {e}",
                    user_message="生成结果过大，请联系管理员检查。",
                ) from e
            path = dest_dir / f"{filename_stem}.png"
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
            path = dest_dir / f"{filename_stem}.png"
            path.write_bytes(raw)
            return path

        if url:
            content = await self.download_bytes(
                str(url), max_bytes=max_bytes
            )
            ext = ".png"
            mime = sniff_image_mime(content)
            if mime == "image/jpeg":
                ext = ".jpg"
            elif mime == "image/webp":
                ext = ".webp"
            else:
                lower = str(url).lower()
                if ".jpg" in lower or ".jpeg" in lower:
                    ext = ".jpg"
                elif ".webp" in lower:
                    ext = ".webp"
            path = dest_dir / f"{filename_stem}{ext}"
            path.write_bytes(content)
            return path

        raise Adobe2APIError("no image data to save")
