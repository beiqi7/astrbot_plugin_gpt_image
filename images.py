"""
从 AstrMessageEvent 提取参考图。

专为 **NapCat + AstrBot（aiocqhttp / OneBot v11）** 优化：

NapCat 图片段常见形态：
  {"type":"image","data":{"file":"ABCD1234.jpg","url":"http://127.0.0.1:xxxx/...","subType":0}}

AstrBot 适配器会变成 Image(file=..., url=...)。

取图顺序（与官方组件 API 一致，并补 NapCat 缺口）：
  1. Image.convert_to_base64()     # 文档推荐，内部 MediaResolver
  2. Image.convert_to_file_path()
  3. 若 file 像缓存名 / 或有 bot：OneBot get_image(file=...)
  4. url / file 为 http(s) 时下载（含 NapCat 本机反代 URL）
  5. 本地路径 file:// 或绝对路径

最终一律输出 data:image/...;base64,...，避免 adobe2api 访问不到
NapCat 的 127.0.0.1 或 QQ 图床。
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import aiohttp

from astrbot.api import logger

# NapCat / go-cqhttp 缓存文件名：一串字母数字 + 扩展名，不是路径也不是 URL
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


def _looks_like_image_bytes(data: bytes) -> bool:
    if not data or len(data) < 8:
        return False
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return True
    if data[:2] == b"\xff\xd8":
        return True
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return True
    if data[:4] == b"RIFF" and len(data) >= 12 and data[8:12] == b"WEBP":
        return True
    return False


def _mime_from_bytes(data: bytes) -> str:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"


def _bytes_to_data_url(data: bytes, mime: str | None = None) -> str:
    mime = mime or _mime_from_bytes(data)
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"


def _pure_b64_to_data_url(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith("data:image") and ";base64," in s:
        try:
            payload = s.split(";base64,", 1)[1]
            data = base64.b64decode(payload, validate=False)
            if _looks_like_image_bytes(data):
                return s
        except Exception:
            return None
    if s.startswith("base64://"):
        s = s[len("base64://") :]
    # NapCat file_id 绝不能当 base64 解
    if _NAPCAT_FILE_ID_RE.match(s):
        return None
    if len(s) < 64:  # 太短不可能是图
        return None
    s = re.sub(r"\s+", "", s)
    try:
        data = base64.b64decode(s, validate=False)
    except Exception:
        return None
    if not _looks_like_image_bytes(data):
        return None
    return _bytes_to_data_url(data)


def _is_napcat_file_id(token: str) -> bool:
    t = (token or "").strip()
    if not t or t.startswith(("http://", "https://", "file://", "base64://", "data:")):
        return False
    # 纯文件名缓存 id
    if _NAPCAT_FILE_ID_RE.match(t):
        return True
    # 无扩展名的长 hash
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
        logger.debug(f"OneBot/NapCat {action} 失败: {e} | keys={list(payload)}")
    return None


async def _napcat_get_image(event, file_token: str) -> str | None:
    """
    NapCat / go-cqhttp: get_image
    返回可能含 file(本地路径)、url、base64
    """
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
        # NapCat 文档/兼容：参数名 file
        for kwargs in ({"file": token}, {"file_id": token}):
            resp = await _onebot_call_action(event, "get_image", **kwargs)
            if not isinstance(resp, dict):
                continue
            logger.debug(f"NapCat get_image 返回 keys={list(resp.keys())}")

            for key in ("base64", "data"):
                if resp.get(key):
                    data_url = _pure_b64_to_data_url(str(resp[key]))
                    if data_url:
                        logger.info("NapCat get_image: base64 成功")
                        return data_url

            for key in ("file", "path", "file_path"):
                fpath = resp.get(key)
                if not fpath:
                    continue
                p = Path(str(fpath))
                if p.is_file():
                    raw = p.read_bytes()
                    if _looks_like_image_bytes(raw):
                        logger.info(f"NapCat get_image: 本地文件 {p}")
                        return _bytes_to_data_url(raw)

            url = resp.get("url")
            if url and str(url).startswith("http"):
                got = await _http_download_image(str(url))
                if got:
                    logger.info("NapCat get_image: url 下载成功")
                    return got
    return None


async def _http_download_image(url: str) -> str | None:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    try:
        host = (urlparse(url).netloc or "").lower()
        if host:
            headers["Referer"] = f"http://{host}/"
        if any(x in host for x in ("qpic.cn", "qq.com", "myqcloud.com", "gtimg.cn")):
            headers["Referer"] = "https://qun.qq.com/"
            headers["Origin"] = "https://qun.qq.com"
    except Exception:
        pass

    timeout = aiohttp.ClientTimeout(total=30, connect=10)
    try:
        async with aiohttp.ClientSession(headers=headers, trust_env=True) as session:
            async with session.get(url, timeout=timeout, allow_redirects=True) as resp:
                if resp.status != 200:
                    logger.warning(f"下载图片 HTTP {resp.status}: {url[:120]}")
                    return None
                data = await resp.read()
                if not _looks_like_image_bytes(data):
                    logger.warning(f"下载内容非图片: {url[:120]}")
                    return None
                mime = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
                if not mime.startswith("image/"):
                    mime = _mime_from_bytes(data)
                return _bytes_to_data_url(data, mime)
    except Exception as e:
        logger.warning(f"下载图片异常: {e} | {url[:120]}")
        return None


async def image_component_to_data_url(comp: Any, event=None) -> str | None:
    file_field = str(getattr(comp, "file", None) or "").strip()
    url_field = str(getattr(comp, "url", None) or "").strip()
    path_field = str(getattr(comp, "path", None) or "").strip()

    logger.info(
        f"解析图片组件: file={file_field[:100]!r} url={url_field[:100]!r} "
        f"path={path_field[:80]!r} has_bot={bool(getattr(event, 'bot', None))}"
    )

    # —— 1) AstrBot 官方：convert_to_base64 ——
    if hasattr(comp, "convert_to_base64"):
        try:
            raw = await comp.convert_to_base64()
            data_url = _pure_b64_to_data_url(str(raw or ""))
            if data_url:
                logger.info("取图成功: convert_to_base64")
                return data_url
            logger.warning("convert_to_base64 返回了无法识别的数据，继续回退")
        except Exception as e:
            logger.warning(f"convert_to_base64 失败: {e}")

    # —— 2) convert_to_file_path ——
    if hasattr(comp, "convert_to_file_path"):
        try:
            path = await comp.convert_to_file_path()
            p = Path(str(path)) if path else None
            if p and p.is_file():
                raw = p.read_bytes()
                if _looks_like_image_bytes(raw):
                    logger.info(f"取图成功: convert_to_file_path -> {p}")
                    return _bytes_to_data_url(raw)
        except Exception as e:
            logger.warning(f"convert_to_file_path 失败: {e}")

    # —— 3) NapCat：file 为缓存名时必须 get_image ——
    if event is not None and getattr(event, "bot", None) is not None:
        for token in (file_field, url_field, path_field):
            if not token:
                continue
            # 缓存名优先；http 也试一次 get_image（部分端支持）
            if _is_napcat_file_id(token) or token.startswith("http"):
                got = await _napcat_get_image(event, token)
                if got:
                    return got
            elif not token.startswith(("data:", "base64://")):
                got = await _napcat_get_image(event, token)
                if got:
                    return got

    # —— 4) 已是 data/base64 ——
    for token in (file_field, url_field, path_field):
        data_url = _pure_b64_to_data_url(token)
        if data_url:
            return data_url

    # —— 5) 本地路径 ——
    for token in (path_field, file_field, url_field):
        if not token:
            continue
        local = token[7:] if token.startswith("file://") else token
        try:
            p = Path(local)
            if p.is_file():
                raw = p.read_bytes()
                if _looks_like_image_bytes(raw):
                    return _bytes_to_data_url(raw)
        except Exception:
            continue

    # —— 6) HTTP 下载（NapCat url 常为 http://127.0.0.1:端口/...）——
    for token in (url_field, file_field):
        if token.startswith("http://") or token.startswith("https://"):
            got = await _http_download_image(token)
            if got:
                logger.info(f"取图成功: HTTP {token[:80]}")
                return got

    logger.error(
        f"NapCat 取图失败 file={file_field[:120]!r} url={url_field[:120]!r} "
        f"bot={type(getattr(event, 'bot', None)).__name__}"
    )
    return None


def iter_message_components(event) -> list[Any]:
    """当前消息 + Reply.chain（AstrBot OneBot 适配器会 get_msg 展开引用图）。"""
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
) -> list[str]:
    max_images = max(1, min(int(max_images or 3), 8))
    results: list[str] = []
    seen: set[str] = set()

    comps = iter_message_components(event)
    platform = ""
    try:
        platform = str(
            getattr(getattr(event, "get_platform_name", lambda: "")(), None)
            or getattr(getattr(event, "platform_meta", None), "name", "")
            or ""
        )
    except Exception:
        platform = ""

    logger.info(
        f"[NapCat取图] platform={platform!r} comps={len(comps)} "
        f"image_like={sum(1 for c in comps if _is_image_like(c))} "
        f"bot={type(getattr(event, 'bot', None)).__name__}"
    )

    for comp in comps:
        if not _is_image_like(comp):
            continue
        data_url = await image_component_to_data_url(comp, event=event)
        if not data_url:
            continue
        key = data_url[:180]
        if key in seen:
            continue
        seen.add(key)
        results.append(data_url)
        logger.info(f"[NapCat取图] 成功 #{len(results)} len={len(data_url)}")
        if len(results) >= max_images:
            break

    if not results:
        logger.warning("[NapCat取图] 未得到任何参考图 data URL")
    return results
