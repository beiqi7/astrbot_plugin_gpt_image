"""AstrBot 插件：通过 adobe2api 调用 Firefly GPT Image 生图。"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Optional
from urllib.parse import urlparse

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api.message_components import Image, Plain, Reply
except ImportError:  # pragma: no cover
    try:
        from astrbot.api.all import Image, Plain, Reply  # type: ignore
    except ImportError:
        from astrbot.api.message_components import Image, Plain  # type: ignore

        Reply = None  # type: ignore

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except Exception:  # pragma: no cover
    def get_astrbot_data_path() -> str:
        return str(Path(__file__).resolve().parent / "data")

from .analyzer import AnalyzeResult, heuristic_size, llm_analyze, parse_user_overrides
from .client import Adobe2APIClient, Adobe2APIError
from .constants import (
    GPT_IMAGE_RATIOS,
    HELP_TEXT,
    RESOLUTIONS,
    build_model_id,
    nearest_ratio,
    parse_ratio_token,
)
from .images import collect_reference_data_urls, count_image_like, probe_image_size
from .quota import DailyQuota, today_key
from .security import (
    DEFAULT_MAX_IMAGE_PIXELS,
    DEFAULT_MAX_OUTPUT_BYTES,
    DEFAULT_MAX_SINGLE_IMAGE_BYTES,
    PathPolicy,
    UrlPolicy,
    redact_path,
    redact_prompt,
    redact_url,
)


# 指令前缀：支持 /gpt图xxx（中文后无空格）—— AstrBot 标准 CommandFilter 要求 "cmd "
_CMD_GEN_RE = re.compile(
    r"^[/!！.．]?(?:gpt图|gptimage|gimg|gptimg|gpt_image)(?=$|[\s,，:：]|[\u4e00-\u9fff])",
    re.IGNORECASE,
)
_CMD_EDIT_RE = re.compile(
    r"^[/!！.．]?(?:gpt改图|gpt编辑|gptedit|gpt_edit|gedit|改图)(?=$|[\s,，:：]|[\u4e00-\u9fff])",
    re.IGNORECASE,
)
_CMD_QUOTA_RE = re.compile(
    r"^[/!！.．]?(?:gpt图次数|gptimagequota|gimgquota|gpt额度)\s*$",
    re.IGNORECASE,
)
_CMD_HELP_RE = re.compile(
    r"^[/!！.．]?(?:gpt图帮助|gptimagehelp|gimghelp)\s*$",
    re.IGNORECASE,
)


@register(
    "astrbot_plugin_gpt_image",
    "serenite",
    "adobe2api GPT Image 生图/改图：自动分辨率、内容审核、每日次数限制",
    "1.6.0",
)
class GptImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self._url_policy = None
        self._user_url_policy = None
        self._max_single_image_bytes = DEFAULT_MAX_SINGLE_IMAGE_BYTES
        self._max_output_bytes = DEFAULT_MAX_OUTPUT_BYTES
        self._rebuild_policies()
        # Client is built lazily in initialize() to avoid double construction
        # (initialize -> _reload_client_if_needed would create a second one).
        self.client = None  # type: ignore[assignment]
        plugin_data = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_gpt_image"
        plugin_data.mkdir(parents=True, exist_ok=True)
        self.data_dir = plugin_data / "output"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.quota = DailyQuota(plugin_data / "daily_quota.json")
        self._bg_tasks: set[asyncio.Task] = set()

        # Concurrency control. We maintain our own counters instead of
        # peeking at asyncio.Semaphore private attributes (_value etc.),
        # which are CPython implementation details and may break across
        # versions.
        self._global_sem: Optional[asyncio.Semaphore] = None
        self._user_sems: dict[str, asyncio.Semaphore] = {}
        self._group_sems: dict[str, asyncio.Semaphore] = {}
        # Admission: BoundedSemaphore caps total in-flight tasks.
        self._admission_sem: Optional[asyncio.BoundedSemaphore] = None
        # Running counts for reaping idle user/group semaphores.
        self._user_active: dict[str, int] = {}
        self._group_active: dict[str, int] = {}
        self._admission_count = 0
        self._queued_lock: Optional[asyncio.Lock] = None

    def _rebuild_policies(self) -> None:
        """Build URL/path policies + size limits from config.

        Four URL policies are created:
        - _url_policy: for NapCat get_image callback URLs (may include
          loopback whitelist from napcat_hosts).
        - _user_url_policy: for URLs coming from the message component
          itself (Image.url / Image.file). Strict: NEVER allows loopback
          or private ranges, regardless of napcat_hosts config. Defends
          against SSRF via attacker-crafted CQ Image nodes.
        - _api_url_policy: for adobe2api requests. Same-origin as
          base_url only. Allows LAN / loopback hosts the operator
          explicitly configured as base_url, even when those would be
          rejected by the generic policy.
        - _output_url_policy: for downloading generated result images.
          Same-origin as base_url only.
        """
        napcat_hosts = str(
            self._cfg("napcat_hosts", "127.0.0.1 localhost ::1")
            or "127.0.0.1 localhost ::1"
        )
        image_suffixes = str(
            self._cfg(
                "image_host_suffixes",
                "qpic.cn qq.com myqcloud.com gtimg.cn",
            )
            or "qpic.cn qq.com myqcloud.com gtimg.cn"
        )
        # NapCat callback policy: allows configured loopback host:port
        self._url_policy = UrlPolicy.from_config(
            napcat_hosts=napcat_hosts,
            image_host_suffixes=image_suffixes,
            allow_public_http=bool(self._cfg("allow_public_http", False)),
        )
        # User-provided Image URL policy: strict, no loopback/private,
        # AND only accepts whitelisted domain suffixes (qpic.cn etc.).
        # Defends against SSRF via attacker-crafted CQ Image nodes and
        # prevents the bot from being abused as an arbitrary public
        # HTTPS proxy.
        self._user_url_policy = UrlPolicy.from_config(
            napcat_hosts="",  # explicit: no loopback whitelist
            image_host_suffixes=image_suffixes,
            allow_public_http=bool(self._cfg("allow_public_http", False)),
            strict_public_only=True,
            allow_other_public_https=False,
        )
        # base_url host:port (computed once, used by both api + output
        # policies). This is what lets operators point at a LAN address
        # like http://192.168.1.20:6001 without the ValidatingResolver
        # rejecting it as a private IP.
        base_host_port = ""
        allow_api_http = bool(self._cfg("allow_insecure_api_http", False))
        try:
            base_parsed = urlparse(str(self._cfg("base_url", "") or ""))
            base_host = (base_parsed.hostname or "").lower()
            if base_host:
                base_port = base_parsed.port
                if base_port is None:
                    base_port = 443 if base_parsed.scheme == "https" else 80
                base_host_port = f"{base_host}:{base_port}"
        except Exception as e:
            logger.warning(f"[gpt_image] failed to parse base_url: {e}")
        # API policy: only base_url's exact host:port. allow_public_http
        # mirrors allow_insecure_api_http so a private/LAN base_url can
        # be reached over plain HTTP when the operator opted in.
        self._api_url_policy = UrlPolicy.from_config(
            napcat_hosts=base_host_port,
            image_host_suffixes="",
            allow_public_http=allow_api_http,
        )
        # Output image policy: allows base_url's exact host:port AND any
        # public HTTPS host (adobe2api may return output URLs on arbitrary
        # CDNs). API key auth headers are only sent to same-origin (see
        # client._download_headers), so this does not leak credentials.
        self._output_url_policy = UrlPolicy.from_config(
            napcat_hosts=base_host_port,
            image_host_suffixes=image_suffixes,
            allow_public_http=False,
        )
        self._path_policy = PathPolicy.from_config(
            allowed_media_dirs=str(self._cfg("allowed_media_dirs", "") or ""),
        )
        try:
            self._max_single_image_bytes = max(
                1024,
                min(
                    int(
                        self._cfg("max_single_image_bytes", DEFAULT_MAX_SINGLE_IMAGE_BYTES)
                        or DEFAULT_MAX_SINGLE_IMAGE_BYTES
                    ),
                    100 * 1024 * 1024,
                ),
            )
        except Exception:
            self._max_single_image_bytes = DEFAULT_MAX_SINGLE_IMAGE_BYTES
        try:
            self._max_output_bytes = max(
                1024,
                min(
                    int(
                        self._cfg("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)
                        or DEFAULT_MAX_OUTPUT_BYTES
                    ),
                    200 * 1024 * 1024,
                ),
            )
        except Exception:
            self._max_output_bytes = DEFAULT_MAX_OUTPUT_BYTES

    # ------------------------------------------------------------------
    # config helpers
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            return self.config.get(key, default)
        except Exception:
            return default

    def _build_client(self) -> Adobe2APIClient:
        client = Adobe2APIClient(
            base_url=str(self._cfg("base_url", "") or ""),
            api_key=str(self._cfg("api_key", "") or ""),
            timeout=max(10.0, min(float(self._cfg("request_timeout", 300) or 300), 3600)),
            max_retries=max(0, min(int(self._cfg("max_retries", 1) or 0), 10)),
            retry_backoff=max(0.5, min(float(self._cfg("retry_backoff", 2) or 2), 60)),
            # API session uses the api policy (whitelists base_url host:port
            # so LAN/loopback deployments work). Output downloads use the
            # separate output policy.
            url_policy=getattr(self, "_api_url_policy", None),
        )
        client.set_max_output_bytes(getattr(self, "_max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES))
        client.set_allow_insecure_http(bool(self._cfg("allow_insecure_api_http", False)))
        client.set_output_url_policy(getattr(self, "_output_url_policy", None))
        return client

    def _reload_client_if_needed(self) -> None:
        self._rebuild_policies()
        new = self._build_client()
        old = self.client
        # First call after __init__ (client is None): just install new.
        if old is None:
            new.set_url_policy(self._api_url_policy)
            new.set_output_url_policy(self._output_url_policy)
            new.set_max_output_bytes(self._max_output_bytes)
            new.set_allow_insecure_http(
                bool(self._cfg("allow_insecure_api_http", False))
            )
            self.client = new
            return
        changed = (
            new.base_url != old.base_url
            or new.api_key != old.api_key
            or abs(new.timeout - old.timeout) > 1e-6
            or new.max_retries != old.max_retries
            or abs(new.retry_backoff - old.retry_backoff) > 1e-6
        )
        if not changed:
            old.set_url_policy(self._api_url_policy)
            old.set_output_url_policy(self._output_url_policy)
            old.set_max_output_bytes(self._max_output_bytes)
            old.set_allow_insecure_http(
                bool(self._cfg("allow_insecure_api_http", False))
            )
            return
        new.set_url_policy(self._api_url_policy)
        new.set_output_url_policy(self._output_url_policy)
        new.set_max_output_bytes(self._max_output_bytes)
        new.set_allow_insecure_http(
            bool(self._cfg("allow_insecure_api_http", False))
        )
        self.client = new
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            try:
                loop.create_task(old.close())
            except Exception:
                pass

    async def initialize(self):
        self._rebuild_policies()
        self._reload_client_if_needed()
        limit = self._daily_limit_value()
        if self.client.configured():
            logger.info(
                f"[gpt_image] loaded: {redact_url(self.client.base_url)} "
                f"(daily_limit={limit}, retries={self.client.max_retries})"
            )
        else:
            logger.warning("[gpt_image] base_url not configured")

    async def terminate(self):
        for task in list(self._bg_tasks):
            task.cancel()
        await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks.clear()
        if self.client is not None:
            await self.client.close()

    # ------------------------------------------------------------------
    # permission / admin / quota
    # ------------------------------------------------------------------

    def _whitelist_ids(self) -> set[str]:
        raw = str(self._cfg("allowed_users", "") or "")
        parts = re.split(r"[\s,;，；]+", raw)
        return {p.strip() for p in parts if p.strip()}

    def _denied_user_ids(self) -> set[str]:
        raw = str(self._cfg("denied_users", "") or "")
        parts = re.split(r"[\s,;，；]+", raw)
        return {p.strip() for p in parts if p.strip()}

    def _allowed_groups(self) -> set[str]:
        raw = str(self._cfg("allowed_groups", "") or "")
        parts = re.split(r"[\s,;，；]+", raw)
        return {p.strip() for p in parts if p.strip()}

    def _denied_groups(self) -> set[str]:
        raw = str(self._cfg("denied_groups", "") or "")
        parts = re.split(r"[\s,;，；]+", raw)
        return {p.strip() for p in parts if p.strip()}

    def _group_id(self, event: AstrMessageEvent) -> str:
        """Return group id for group messages, empty string for private."""
        for attr in ("get_group_id", "group_id"):
            try:
                v = getattr(event, attr, None)
                if callable(v):
                    v = v()
                if v is not None and str(v).strip():
                    return str(v).strip()
            except Exception:
                continue
        try:
            mo = getattr(event, "message_obj", None)
            gid = getattr(mo, "group_id", None)
            if gid is not None and str(gid).strip():
                return str(gid).strip()
        except Exception:
            pass
        return ""

    def _is_private_chat(self, event: AstrMessageEvent) -> bool:
        return not self._group_id(event)

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        try:
            return bool(event.is_admin())
        except Exception:
            return False

    def _user_id(self, event: AstrMessageEvent) -> str:
        return str(event.get_sender_id() or "").strip() or "unknown"

    def _daily_limit_value(self) -> int:
        """普通用户每日上限。负数视为 0；管理员不走此限额。"""
        try:
            return max(0, int(self._cfg("daily_limit", 5) or 0))
        except Exception:
            return 5

    def _quota_limit_for(self, event: AstrMessageEvent) -> int:
        """返回该用户的日限额；-1 表示无限。"""
        if self._is_admin(event):
            return -1
        return self._daily_limit_value()

    def _check_permission(self, event: AstrMessageEvent) -> tuple[bool, str]:
        is_admin = self._is_admin(event)
        # Admin always bypasses group/private/user restrictions
        if is_admin:
            return True, ""

        # Personal blacklist: highest priority among non-admin checks.
        sender = self._user_id(event)
        if sender and sender in self._denied_user_ids():
            return False, "⛔ 你已被禁止使用该功能。"

        # Private chat gate
        if self._is_private_chat(event):
            if not bool(self._cfg("allow_private_chat", True)):
                return False, "⛔ 私聊已禁用，请在允许的群聊中使用。"
        else:
            gid = self._group_id(event)
            denied = self._denied_groups()
            if gid and gid in denied:
                return False, "⛔ 本群已被禁用该功能。"
            allowed = self._allowed_groups()
            if allowed and gid not in allowed:
                return False, "⛔ 本群不在允许使用的名单中。"

        mode = str(self._cfg("permission_mode", "all") or "all").lower()
        if mode in ("", "all", "everyone", "public"):
            return True, ""

        if mode in ("admin", "admins", "管理员"):
            return False, "⛔ 此指令仅 AstrBot 全局管理员可用。"

        if mode in ("whitelist", "wl", "白名单"):
            if sender and sender in self._whitelist_ids():
                return True, ""
            return False, "⛔ 你不在白名单中，无法使用 GPT Image 生图。"

        return True, ""

    def _check_quota(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """生图前检查次数。管理员直接通过。"""
        limit = self._quota_limit_for(event)
        if limit < 0:
            return True, ""

        uid = self._user_id(event)
        ok, used, lim = self.quota.can_use(uid, limit)
        if ok:
            return True, ""
        return (
            False,
            f"今日次数已用完（{used}/{lim}），请明天再试。",
        )

    def _quota_status_text(self, event: AstrMessageEvent) -> str:
        uid = self._user_id(event)
        limit = self._quota_limit_for(event)
        used = self.quota.get_used(uid)
        date = today_key()
        if limit < 0:
            # 不暴露特权信息
            return f"今日额度（{date}）\n已用：{used} 次"
        remain = max(0, limit - used)
        return (
            f"今日额度（{date}）\n"
            f"已用：{used}/{limit}\n"
            f"剩余：{remain} 次"
        )

    def _format_user_error(self, err: Exception) -> str:
        if isinstance(err, Adobe2APIError):
            msg = (err.user_message or "").strip() or "生图失败，请稍后再试。"
            return f"❌ {msg}"
        return "❌ 生图失败，请稍后再试。"

    # ------------------------------------------------------------------
    # message helpers
    # ------------------------------------------------------------------

    def _raw_message_text(self, event: AstrMessageEvent) -> str:
        return (
            getattr(event, "message_str", None) or event.get_message_str() or ""
        ).strip()

    def _extract_prompt_text(self, event: AstrMessageEvent, *, edit: bool = False) -> str:
        """
        Strip command prefix. Supports `/gpt图她换装` (no space after Chinese command).
        Only strips configured aliases if no built-in command was matched.
        """
        text = self._raw_message_text(event)
        original = text
        if edit:
            text = re.sub(
                r"^[/!！.．]?(?:gpt改图|gpt编辑|gptedit|gedit|gpt_edit|改图)\s*",
                "",
                text,
                count=1,
                flags=re.IGNORECASE,
            )
        else:
            text = re.sub(
                r"^[/!！.．]?(?:gptimage|gimg|gptimg|gpt_image)\s*",
                "",
                text,
                count=1,
                flags=re.IGNORECASE,
            )
            text = re.sub(
                r"^[/!！.．]?gpt图\s*",
                "",
                text,
                count=1,
                flags=re.IGNORECASE,
            )
        # Only strip aliases if no built-in command was matched
        if text == original:
            aliases = str(self._cfg("command_alias", "") or "")
            for alias in re.split(r"[\s,;，；]+", aliases):
                alias = alias.strip()
                if not alias:
                    continue
                text = re.sub(
                    rf"^[/!！.．]?{re.escape(alias)}\s*",
                    "",
                    text,
                    count=1,
                    flags=re.IGNORECASE,
                )
        return text.strip()

    def _should_handle_as_command(self, event: AstrMessageEvent) -> bool:
        """正则入口不受 wake 限制，自行判断是否接管。"""
        try:
            if getattr(event, "is_at_or_wake_command", False):
                return True
        except Exception:
            pass
        text = self._raw_message_text(event)
        if re.match(r"^[/!！.．]", text):
            return True
        # 带图且以指令开头
        if count_image_like(event) > 0 and (
            _CMD_GEN_RE.search(text) or _CMD_EDIT_RE.search(text)
        ):
            return True
        return False

    def _stop_other_handlers(self, event: AstrMessageEvent) -> None:
        """阻止主 Agent 继续处理同一条消息。"""
        try:
            event.stop_event()
        except Exception:
            pass

    def _user_message_id(self, event: AstrMessageEvent) -> str | int | None:
        """用户原消息 ID，用于构造引用回复。"""
        try:
            mid = getattr(getattr(event, "message_obj", None), "message_id", None)
            if mid is not None and str(mid).strip() != "":
                return mid
        except Exception:
            pass
        for name in ("get_message_id", "message_id"):
            try:
                attr = getattr(event, name, None)
                mid = attr() if callable(attr) else attr
                if mid is not None and str(mid).strip() != "":
                    return mid
            except Exception:
                continue
        return None

    def _with_user_quote(
        self, event: AstrMessageEvent, components: list
    ) -> list:
        """在消息链头部插入 Reply，引用用户原消息（OneBot/NapCat 会显示为回复）。"""
        chain = list(components or [])
        if Reply is None:
            return chain
        mid = self._user_message_id(event)
        if mid is None:
            return chain
        # 避免重复 Reply
        if chain and type(chain[0]).__name__ == "Reply":
            return chain
        try:
            return [Reply(id=mid), *chain]
        except Exception as e:
            logger.debug(f"构造 Reply 失败: {e}")
            return chain

    def _quoted_plain(self, event: AstrMessageEvent, text: str):
        """带引用的纯文本 chain_result，用于错误/提示回复。"""
        return event.chain_result(self._with_user_quote(event, [Plain(text)]))

    async def _notify(self, event: AstrMessageEvent, text: str) -> None:
        """
        中间状态用 send 直发，避免多次 yield 被其它插件
        （如 recall_cancel 的 on_decorating_result）截断后续结果。
        进度提示同样引用用户消息，方便群里对上号。
        """
        try:
            comps = self._with_user_quote(event, [Plain(text)])
            await event.send(MessageChain(chain=comps))
        except Exception as e:
            logger.warning(f"发送进度提示失败: {e}")

    def _max_ref_images(self) -> int:
        try:
            n = int(self._cfg("max_ref_images", 3) or 3)
        except Exception:
            n = 3
        return max(1, min(n, 8))

    async def _collect_image_data_urls(self, event: AstrMessageEvent) -> list[str]:
        """Extract reference images from current + quoted message as data URLs."""
        return await collect_reference_data_urls(
            event,
            max_images=self._max_ref_images(),
            url_policy=self._url_policy,
            user_url_policy=self._user_url_policy,
            path_policy=self._path_policy,
            max_single_bytes=self._max_single_image_bytes,
            max_pixels=DEFAULT_MAX_IMAGE_PIXELS,
            reject_animated=bool(self._cfg("reject_animated_images", True)),
        )

    async def _cleanup_old_files(self, minutes: int = 30) -> None:
        cutoff = time.time() - minutes * 60
        try:
            for p in self.data_dir.glob("gpt_image_*"):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"清理临时图失败: {e}")

    # ------------------------------------------------------------------
    # concurrency control
    # ------------------------------------------------------------------

    def _cfg_int(self, key: str, default: int, *, lo: int = 1, hi: int = 1024) -> int:
        # NOTE: `or default` would turn a legitimate 0 into the default,
        # so we treat empty/None specially.
        raw = self._cfg(key, default)
        if raw is None or raw == "":
            v = int(default)
        else:
            try:
                v = int(raw)
            except Exception:
                v = int(default)
        return max(lo, min(v, hi))

    def _global_capacity(self) -> int:
        return self._cfg_int("max_concurrent_global", 2, lo=1, hi=64)

    def _user_capacity(self) -> int:
        return self._cfg_int("max_concurrent_per_user", 1, lo=1, hi=64)

    def _group_capacity(self) -> int:
        return self._cfg_int("max_concurrent_per_group", 1, lo=1, hi=64)

    def _queue_capacity(self) -> int:
        # Number of tasks that may sit waiting for semaphores in addition
        # to the ones currently running. lo=0 means "no queue at all;
        # accept only what can start immediately".
        return self._cfg_int("max_queue_length", 10, lo=0, hi=1024)

    def _admission_capacity(self) -> int:
        # Total in-flight tasks allowed (running + waiting).
        return max(1, self._global_capacity() + self._queue_capacity())

    def _ensure_concurrency(self) -> None:
        if self._global_sem is None:
            self._global_sem = asyncio.Semaphore(self._global_capacity())
        if self._queued_lock is None:
            self._queued_lock = asyncio.Lock()

    def _get_user_sem(self, uid: str) -> asyncio.Semaphore:
        sem = self._user_sems.get(uid)
        if sem is None:
            sem = asyncio.Semaphore(self._user_capacity())
            self._user_sems[uid] = sem
        return sem

    def _get_group_sem(self, gid: str) -> asyncio.Semaphore:
        sem = self._group_sems.get(gid)
        if sem is None:
            sem = asyncio.Semaphore(self._group_capacity())
            self._group_sems[gid] = sem
        return sem

    def _inc_user(self, uid: str) -> None:
        self._user_active[uid] = self._user_active.get(uid, 0) + 1

    def _inc_group(self, gid: str) -> None:
        self._group_active[gid] = self._group_active.get(gid, 0) + 1

    def _dec_user(self, uid: str) -> None:
        v = self._user_active.get(uid, 0) - 1
        if v <= 0:
            self._user_active.pop(uid, None)
        else:
            self._user_active[uid] = v
        self._maybe_reap_sems()

    def _dec_group(self, gid: str) -> None:
        v = self._group_active.get(gid, 0) - 1
        if v <= 0:
            self._group_active.pop(gid, None)
        else:
            self._group_active[gid] = v
        self._maybe_reap_sems()

    _SEMS_REAP_THRESHOLD = 200

    def _maybe_reap_sems(self) -> None:
        """Lazy cleanup of idle user/group semaphore entries.

        Only runs when the dicts grow past a threshold; removes entries
        whose active count is 0 (no one holding or waiting).
        """
        if len(self._user_sems) > self._SEMS_REAP_THRESHOLD:
            idle = [uid for uid in self._user_sems if uid not in self._user_active]
            for uid in idle:
                self._user_sems.pop(uid, None)
        if len(self._group_sems) > self._SEMS_REAP_THRESHOLD:
            idle = [gid for gid in self._group_sems if gid not in self._group_active]
            for gid in idle:
                self._group_sems.pop(gid, None)

    async def _try_admit(self) -> bool:
        """Try to acquire an admission slot without waiting.

        Uses a simple counter instead of peeking at Semaphore internals.
        Safe because asyncio is single-threaded: no other coroutine can
        run between the check and the increment (no await in between).
        """
        self._ensure_concurrency()
        capacity = self._admission_capacity()
        if self._admission_count >= capacity:
            return False
        self._admission_count += 1
        return True

    def _release_admission(self) -> None:
        self._admission_count = max(0, self._admission_count - 1)

    def _on_bg_task_done(self, task: asyncio.Task) -> None:
        """Cleanup callback for background generation tasks.

        - If the task was cancelled before its coroutine body started
          (finally never ran), refund quota + release admission here.
        - Retrieve and log any unhandled exception so Python doesn't
          warn 'Task exception was never retrieved'.
        """
        self._bg_tasks.discard(task)
        if task.cancelled():
            # If the coroutine body already ran its finally block, it
            # set _gpt_cleaned = True and we must NOT do a second
            # refund + admission release (would double-count).
            if getattr(task, "_gpt_cleaned", False):
                return
            # The coroutine body never started (or finally didn't run).
            # Do the cleanup here using metadata stashed on the task.
            uid = getattr(task, "_gpt_uid", None)
            reserved = getattr(task, "_gpt_reserved", False)
            res_date = getattr(task, "_gpt_res_date", "")
            if uid and reserved:
                try:
                    self.quota.refund(uid, reservation_date=res_date)
                    logger.info(
                        f"[gpt_image] refunded quota for cancelled task user={uid}"
                    )
                except Exception as e:
                    logger.warning(f"[gpt_image] refund for cancelled task failed: {e}")
            self._release_admission()
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        except Exception:
            return
        if exc is not None:
            logger.error(
                f"[gpt_image] background task exited with {type(exc).__name__}: {exc}"
            )

    # ------------------------------------------------------------------
    # core generation pipeline
    # ------------------------------------------------------------------

    async def _analyze_and_build(
        self,
        event: AstrMessageEvent,
        raw_text: str,
        *,
        ref_images: list[str] | None = None,
    ) -> tuple[Optional[AnalyzeResult], str, str, Optional[str]]:
        """
        流水线：
          用户原文 → prompt（生图用，永不改写）
          LLM/规则 → 仅审核 + 选 aspect_ratio；resolution 恒等于配置值
          → model_id

        改图时（ref_images 非空）：
          - 若用户没手动 --ratio，则用参考图的实际宽高比覆盖 LLM/默认比例，
            保持原图画幅（避免出现 1:1 强行改成 16:9 的畸变）。
          - 多张参考图时以第一张为准。

        返回 (analyze, user_prompt, model_id, error_message)
        """
        # user_prompt = 去掉 --ratio 等控制参数后的用户描述正文（语言原样保留）
        user_prompt, overrides = parse_user_overrides(raw_text)
        if not user_prompt:
            return None, "", "", "请附带图片描述，例如：/gpt图 一只在阳光下的柴犬"

        is_admin = self._is_admin(event)
        enable_audit = bool(self._cfg("enable_audit", True))
        if overrides.get("no_audit") and is_admin:
            enable_audit = False

        # auto_select_aspect_ratio: controls LLM-based aspect ratio selection only.
        # Backward compat: read auto_select_aspect_ratio first, fall back to auto_select_size.
        auto_aspect = bool(
            self._cfg("auto_select_aspect_ratio", self._cfg("auto_select_size", True))
        )
        if overrides.get("no_auto"):
            auto_aspect = False

        # resolution_mode is the sole determinant of resolution source.
        res_mode = str(self._cfg("resolution_mode", "fixed") or "fixed").strip().lower()
        if res_mode not in ("fixed", "llm"):
            res_mode = "fixed"
        default_res = str(self._cfg("default_resolution", "2k") or "2k").lower()
        if default_res not in RESOLUTIONS:
            default_res = "2k"
        default_ratio = str(self._cfg("default_aspect_ratio", "1:1") or "1:1")
        if default_ratio not in GPT_IMAGE_RATIOS:
            default_ratio = "1:1"

        manual_ratio = parse_ratio_token(str(overrides.get("ratio") or ""))

        audit_failure_policy = str(
            self._cfg("audit_failure_policy", "block") or "block"
        ).strip().lower()
        if audit_failure_policy not in ("block", "keyword_only", "allow"):
            audit_failure_policy = "block"

        audit_system_prompt = str(self._cfg("audit_prompt", "") or "").strip()
        audit_provider_id = str(self._cfg("audit_provider_id", "") or "").strip()
        # Only send reference images to the audit model when the operator
        # has opted in (audit model must support vision).
        audit_refs: list[str] = []
        if ref_images and bool(self._cfg("audit_reference_images", False)):
            audit_refs = list(ref_images)
        analyze_kwargs = dict(
            umo=getattr(event, "unified_msg_origin", None),
            strict=bool(self._cfg("audit_strict", False)),
            timeout=float(self._cfg("llm_timeout", 45) or 45),
            system_prompt=audit_system_prompt or None,
            enable_keyword_filter=bool(self._cfg("enable_keyword_filter", True)),
            provider_id=audit_provider_id or None,
            audit_failure_policy=audit_failure_policy,
            image_urls=audit_refs,
        )

        # Determine if LLM is needed: for audit, for resolution, or for aspect ratio
        need_llm = (
            enable_audit
            or (res_mode == "llm")
            or (auto_aspect and not manual_ratio)
        )

        if need_llm:
            analyze = await llm_analyze(
                self.context,
                prompt=user_prompt,
                default_res=default_res,
                default_ratio=default_ratio,
                enable_audit=enable_audit,
                **analyze_kwargs,
            )
        else:
            analyze = heuristic_size(
                user_prompt,
                default_res,
                manual_ratio or default_ratio,
            )
            analyze.source = "manual" if manual_ratio else "default"

        if not analyze.allowed:
            return analyze, user_prompt, "", None

        # Aspect ratio post-processing (priority order):
        #   1. manual --ratio
        #   2. ref_images actual ratio (edit mode preserves original aspect)
        #   3. auto_aspect off -> default ratio
        #   4. LLM-selected ratio (already set by llm_analyze)
        if manual_ratio:
            analyze.aspect_ratio = manual_ratio
            if "manual" not in analyze.source:
                analyze.source = f"{analyze.source}+manual"
        elif ref_images:
            ref_ratio = None
            for ref in ref_images:
                size = probe_image_size(ref)
                if size:
                    w, h = size
                    ref_ratio = nearest_ratio(w, h)
                    logger.info(
                        f"[gpt_image] ref image {w}x{h} -> ratio {ref_ratio}"
                    )
                    break
            if ref_ratio:
                analyze.aspect_ratio = ref_ratio
                if "ref" not in analyze.source:
                    analyze.source = f"{analyze.source}+ref"
            elif not auto_aspect:
                analyze.aspect_ratio = default_ratio
        elif not auto_aspect:
            analyze.aspect_ratio = default_ratio

        if analyze.aspect_ratio not in GPT_IMAGE_RATIOS:
            analyze.aspect_ratio = default_ratio

        # 分辨率：fixed 模式无视 LLM 输出；llm 模式使用 LLM 结果，非法回退 default
        if res_mode == "fixed":
            analyze.resolution = default_res
        else:
            if analyze.resolution not in RESOLUTIONS:
                analyze.resolution = default_res

        # model_id 仅由分辨率+画幅决定；prompt 始终是用户原文
        model_id = build_model_id(analyze.resolution, analyze.aspect_ratio)
        return analyze, user_prompt, model_id, None

    async def _run_generate(
        self,
        event: AstrMessageEvent,
        raw_text: str,
        *,
        require_image: bool = False,
        force_edit: bool = False,
    ) -> AsyncGenerator[Any, None]:
        """
        生图 / 改图统一入口。

        - require_image / force_edit: 改图模式，必须有参考图
        - 有参考图时走 adobe2api 图生图（chat completions + image_url）
        - 用户原文仍原样作为 prompt，LLM 只审核+选模型
        """
        # Use the current client (no per-request reload to avoid race conditions).
        # Config changes require plugin reload via AstrBot WebUI.
        client = self.client

        ok, deny = self._check_permission(event)
        if not ok:
            yield self._quoted_plain(event, deny)
            return

        if client is None or not client.configured():
            yield self._quoted_plain(
                event,
                "⚠️ 未配置 adobe2api 地址。请在插件配置中填写 base_url 与 api_key。",
            )
            return

        text = (raw_text or "").strip()
        if not text or text in {"help", "帮助", "?", "？"}:
            yield self._quoted_plain(event, HELP_TEXT)
            return

        # Prompt length limit
        MAX_PROMPT_LEN = 2000
        if len(text) > MAX_PROMPT_LEN:
            yield self._quoted_plain(
                event,
                f"❌ 描述过长（{len(text)} 字），请缩短到 {MAX_PROMPT_LEN} 字以内。",
            )
            return

        # Atomic quota reservation (BEFORE image download so that users
        # without quota can't trigger expensive remote downloads).
        limit = self._quota_limit_for(event)
        reserved = False
        res_date = ""
        if limit >= 0:
            ok_q, _, res_date = self.quota.reserve(self._user_id(event), limit)
            if not ok_q:
                yield self._quoted_plain(
                    event, "今日次数已用完，请明天再试。"
                )
                return
            reserved = True

        # Admission slot (BEFORE image download so the queue limit
        # protects the download phase too).
        admitted = await self._try_admit()
        if not admitted:
            if reserved:
                self.quota.refund(self._user_id(event), reservation_date=res_date)
            yield self._quoted_plain(
                event,
                "⏳ 当前排队人数过多，稍后再试（已退还本次次数）。",
            )
            return

        # --- From here we hold quota + admission. The finally block
        # below cleans up on every exit where the background task has
        # NOT taken ownership (transferred == False). Covers cancel
        # during _notify, image download, analyze, etc. ---
        transferred = False
        try:
            # Download reference images (gated by quota + admission)
            image_like_n = count_image_like(event)
            ref_images = await self._collect_image_data_urls(event)
            is_edit = bool(force_edit or require_image or ref_images)
            if (force_edit or require_image) and not ref_images:
                if image_like_n > 0:
                    yield self._quoted_plain(
                        event,
                        "检测到图片但读取失败。请重新发送原图（不要用表情包缩略图），"
                        "或换一张后重试；回复带图消息时请确认引用的是图片。",
                    )
                else:
                    yield self._quoted_plain(
                        event,
                        "改图需要参考图。请发送图片并配上修改说明，或回复一张图片后使用改图指令。\n"
                        "示例：发图 + /gpt改图 改成水彩风格",
                    )
                return
            if image_like_n > 0 and not ref_images:
                yield self._quoted_plain(
                    event,
                    "消息里好像有图，但没能读到参考图，已取消（避免变成纯文生图）。\n"
                    "请重新发送原图后再试 /gpt改图 或 /gpt图。",
                )
                return

            # Progress notification via send (avoids recall plugins)
            await self._notify(
                event,
                f"⏳ 正在{'改图' if is_edit else '生图'}…（已取到参考图×{len(ref_images)}）"
                if ref_images
                else "⏳ 正在生图…",
            )

            analyze = None
            prompt = ""
            model_id = ""
            err = ""
            try:
                analyze, prompt, model_id, err = await self._analyze_and_build(
                    event, text, ref_images=ref_images or None
                )
            except asyncio.CancelledError:
                logger.warning("[gpt_image] analyze cancelled")
                raise
            except Exception:
                logger.exception("[gpt_image] analyze failed")
                yield self._quoted_plain(event, "❌ 分析失败，请稍后再试。")
                return

            if err:
                yield self._quoted_plain(event, err)
                return
            if analyze is not None and not analyze.allowed:
                logger.info(
                    f"[gpt_image] request blocked user={self._user_id(event)} "
                    f"reason={analyze.reason}"
                )
                yield self._quoted_plain(event, "暂时无法处理该请求，请换一个描述再试。")
                return
            if not model_id or not prompt:
                yield self._quoted_plain(event, "参数解析失败，请换一种描述再试。")
                return

            mode = "改图" if is_edit else "文生图"

            meta_bits = []
            if analyze:
                meta_bits.append(f"{analyze.resolution.upper()} · {analyze.aspect_ratio}")
            meta_bits.append(mode)
            if ref_images:
                meta_bits.append(f"参考图×{len(ref_images)}")
            logger.info(
                f"[gpt_image] start adobe2api model={model_id} mode={mode} "
                f"refs={len(ref_images)} {redact_prompt(prompt)}"
            )
            await self._notify(event, f"🎨 生成中…（{' · '.join(meta_bits)}）")

            # Launch background generation to avoid tool timeout (120s)
            # and keep handler responsive
            task = asyncio.create_task(self._generate_and_deliver(
                event=event,
                client=client,
                prompt=prompt,
                model_id=model_id,
                ref_images=ref_images,
                reserved=reserved,
                res_date=res_date,
                limit=limit,
                analyze=analyze,
                mode=mode,
            ))
            # Stash cleanup metadata on the task so _on_bg_task_done can
            # do refund + admission release if the task is cancelled
            # before its coroutine body even starts (finally won't run).
            task._gpt_uid = self._user_id(event)  # type: ignore[attr-defined]
            task._gpt_reserved = reserved  # type: ignore[attr-defined]
            task._gpt_res_date = res_date  # type: ignore[attr-defined]
            self._bg_tasks.add(task)
            task.add_done_callback(self._on_bg_task_done)
            # Ownership transferred: the background task is now
            # responsible for refunding quota and releasing admission.
            transferred = True
            return
        finally:
            if not transferred:
                if reserved:
                    self.quota.refund(
                        self._user_id(event), reservation_date=res_date
                    )
                self._release_admission()

    async def _generate_and_deliver(
        self,
        event: AstrMessageEvent,
        client: Adobe2APIClient,
        prompt: str,
        model_id: str,
        ref_images: list[str],
        reserved: bool,
        res_date: str,
        limit: int,
        analyze: Optional[AnalyzeResult],
        mode: str,
    ) -> None:
        """Background task: generate image, send result, handle errors.

        Concurrency: acquires per-user, per-group and global semaphores
        in that order (finest-grained first) so one user's queued task
        cannot occupy a global slot while blocked on their per-user cap.

        Admission slot: already acquired by the caller before launching
        this task. We release it exactly once in `finally`.

        Refund invariant: `refund_needed` stays True until the result is
        successfully delivered. Every exit path (cancellation during any
        await, exception, plugin shutdown, send failure) hits `finally`,
        which refunds the quota if still needed, releases only the
        semaphores we actually acquired, and releases the admission slot.
        """

        uid = self._user_id(event)
        gid = self._group_id(event)  # empty for private chats

        self._ensure_concurrency()
        global_sem = self._global_sem
        user_sem = self._get_user_sem(uid)
        group_sem = self._get_group_sem(gid) if gid else None

        acquired_user = False
        acquired_group = False
        acquired_global = False
        admission_released = False
        refund_needed = True

        try:
            # 1) Per-user first: bounds one user's queue depth without
            #    hogging global slots that other users could otherwise use.
            await user_sem.acquire()
            acquired_user = True
            self._inc_user(uid)

            # 2) Per-group next (same rationale for groups).
            if group_sem is not None:
                await group_sem.acquire()
                acquired_group = True
                self._inc_group(gid)

            # 3) Global last. If contended, tell the user we're waiting.
            if global_sem is not None and global_sem.locked():
                try:
                    await self._notify(
                        event, "⏳ 前面有其他生图任务，正在排队…"
                    )
                except Exception:
                    pass
            await global_sem.acquire()  # type: ignore[union-attr]
            acquired_global = True

            try:
                result = await client.generate_image(
                    prompt=prompt,
                    model=model_id,
                    image_data_urls=ref_images or None,
                    timeout=float(self._cfg("request_timeout", 300) or 300),
                )
            except Adobe2APIError as e:
                logger.error(f"[gpt_image] generate failed: {e}")
                await self._notify(event, self._format_user_error(e))
                return
            except asyncio.CancelledError:
                logger.warning("[gpt_image] generate cancelled")
                raise
            except Exception as e:
                logger.exception("[gpt_image] unexpected error")
                await self._notify(event, self._format_user_error(e))
                return

            logger.info(f"[gpt_image] adobe2api ok model={result.get('model')}")

            new_used = self.quota.get_used(uid)

            footer_parts: list[str] = []
            if bool(self._cfg("show_meta", True)) and analyze:
                footer_parts.append(
                    f"{analyze.resolution.upper()} · {analyze.aspect_ratio} · {mode}"
                )
            if limit >= 0:
                remain = max(0, limit - new_used)
                footer_parts.append(
                    f"今日还可生成 {remain} 次（已用 {new_used}/{limit}）"
                )
            footer = "\n".join(footer_parts)

            await self._cleanup_old_files()
            stem = (
                f"gpt_image_{time.strftime('%Y%m%d_%H%M%S')}_"
                f"{uuid.uuid4().hex[:8]}"
            )
            try:
                path = await client.save_result_image(
                    result, self.data_dir, stem,
                    max_bytes=self._max_output_bytes,
                    max_pixels=DEFAULT_MAX_IMAGE_PIXELS,
                )
            except Exception as e:
                logger.warning(f"[gpt_image] save failed: {e}")
                await self._notify(
                    event, "❌ 图片保存失败，已退还次数，请稍后再试。"
                )
                return

            chain = []
            if footer:
                chain.append(Plain(f"✅ 生成完成\n{footer}\n"))
            chain.append(Image.fromFileSystem(str(path)))
            quoted = self._with_user_quote(event, chain)
            try:
                await event.send(MessageChain(chain=quoted))
                logger.info(f"[gpt_image] result sent path={redact_path(path)}")
                # Delivery successful — commit the quota deduction.
                refund_needed = False
            except Exception as e:
                logger.warning(f"[gpt_image] send failed: {e}")
                # refund_needed stays True, finally will refund.
        finally:
            # Refund on every non-delivered exit.
            if refund_needed and reserved:
                try:
                    self.quota.refund(uid, reservation_date=res_date)
                    logger.info(
                        f"[gpt_image] refunded quota user={uid} "
                        f"(not delivered)"
                    )
                except Exception as e:
                    logger.warning(f"[gpt_image] refund failed: {e}")

            # Release only the semaphores we actually acquired,
            # in reverse acquisition order.
            if acquired_global and global_sem is not None:
                global_sem.release()
            if acquired_group and group_sem is not None:
                group_sem.release()
                self._dec_group(gid)
            if acquired_user:
                user_sem.release()
                self._dec_user(uid)

            # Release the admission slot exactly once. This is the
            # single budget that caps running + waiting tasks.
            if not admission_released:
                self._release_admission()
                admission_released = True

            # Mark the task as cleaned-up so _on_bg_task_done doesn't
            # do a second refund + admission release when it sees
            # task.cancelled() == True (which happens when CancelledError
            # propagates out of this finally block).
            try:
                task = asyncio.current_task()
                if task is not None:
                    task._gpt_cleaned = True  # type: ignore[attr-defined]
            except Exception:
                pass

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    #
    # 注意：AstrBot 标准 @filter.command 要求「指令名 + 空格」，
    # 中文常见写法 `/gpt图给她换装`（无空格）匹配失败，会落到主 Agent。
    # 因此用 regex 做主入口（不受 wake_prefix 制约，自行判断是否接管）。

    @filter.regex(
        r"(?is)^[/!！.．]?(?:gpt改图|gpt编辑|gptedit|gpt_edit|gedit|改图|"
        r"gpt图|gptimage|gimg|gptimg|gpt_image|"
        r"gpt图次数|gptimagequota|gimgquota|gpt额度|"
        r"gpt图帮助|gptimagehelp|gimghelp)"
    )
    async def cmd_gpt_entry(self, event: AstrMessageEvent):
        """Unified entry: handles all gpt图* commands via regex.

        Only this regex entry is registered (no @filter.command duplicates)
        to prevent double-execution where regex + command both fire.
        """
        text = self._raw_message_text(event)
        logger.info(
            f"[gpt_image] entry matched images={count_image_like(event)} "
            f"text={redact_prompt(text)}"
        )

        if not self._should_handle_as_command(event):
            logger.info("[gpt_image] not @/wake and not explicit command, skip")
            return

        if _CMD_HELP_RE.match(text):
            self._stop_other_handlers(event)
            yield self._quoted_plain(event, HELP_TEXT)
            return
        if _CMD_QUOTA_RE.match(text):
            self._stop_other_handlers(event)
            ok, deny = self._check_permission(event)
            if not ok:
                yield self._quoted_plain(event, deny)
                return
            yield self._quoted_plain(event, self._quota_status_text(event))
            return

        if _CMD_EDIT_RE.search(text):
            self._stop_other_handlers(event)
            prompt = self._extract_prompt_text(event, edit=True)
            logger.info(f"[gpt_image] edit {redact_prompt(prompt)}")
            async for result in self._run_generate(
                event, prompt, require_image=True, force_edit=True
            ):
                yield result
            return

        if _CMD_GEN_RE.search(text):
            self._stop_other_handlers(event)
            prompt = self._extract_prompt_text(event, edit=False)
            logger.info(f"[gpt_image] gen {redact_prompt(prompt)}")
            async for result in self._run_generate(event, prompt):
                yield result
            return

    @filter.regex(r"(?is)^[/!！.．]\S+")
    async def cmd_alias_entry(self, event: AstrMessageEvent):
        """Fallback handler for user-configured command aliases.

        Early-returns if no aliases are configured, so the broad regex
        has zero overhead for deployments that don't use custom aliases.
        """
        aliases = str(self._cfg("command_alias", "") or "")
        if not aliases.strip():
            return
        text = self._raw_message_text(event)
        if (
            _CMD_GEN_RE.search(text)
            or _CMD_EDIT_RE.search(text)
            or _CMD_QUOTA_RE.match(text)
            or _CMD_HELP_RE.match(text)
        ):
            return

        aliases = str(self._cfg("command_alias", "") or "")
        matched = False
        for alias in re.split(r"[\s,;，；]+", aliases):
            alias = alias.strip()
            if not alias:
                continue
            if re.match(
                rf"^[/!！.．]?{re.escape(alias)}(?=$|[\s,，:：])",
                text,
                re.IGNORECASE,
            ):
                matched = True
                break

        if not matched:
            return
        if not self._should_handle_as_command(event):
            return

        self._stop_other_handlers(event)
        prompt = self._extract_prompt_text(event, edit=False)
        logger.info(f"[gpt_image] alias gen {redact_prompt(prompt)}")
        async for result in self._run_generate(event, prompt):
            yield result

    @filter.llm_tool(name="gpt_image_generate")
    async def tool_gpt_image(
        self,
        event: AstrMessageEvent,
        prompt: str,
        aspect_ratio: str = "",
    ):
        """使用 adobe2api 的 GPT Image 生成图片。消息中若有图则自动按图编辑。

        Args:
            prompt(string): 图片描述或改图说明，必填，保持用户原语言
            aspect_ratio(string): 可选画幅，如 1:1、16:9、9:16
        """
        parts = [prompt or ""]
        if aspect_ratio:
            parts.append(f"--ratio {aspect_ratio}")
        text = " ".join(p for p in parts if p).strip()
        yielded_any = False
        async for result in self._run_generate(event, text):
            yield result
            yielded_any = True
        # Only yield confirmation if _run_generate launched the background
        # task without yielding any error/help message first.
        if not yielded_any:
            yield event.chain_result([Plain("图片生成任务已提交，结果将直接发送到当前会话。")])

    @filter.llm_tool(name="gpt_image_edit")
    async def tool_gpt_edit(
        self,
        event: AstrMessageEvent,
        prompt: str,
        aspect_ratio: str = "",
    ):
        """使用 adobe2api 的 GPT Image 编辑图片。需用户消息中附带或引用参考图。

        Args:
            prompt(string): 改图说明（用户原语言），必填
            aspect_ratio(string): 可选画幅
        """
        parts = [prompt or ""]
        if aspect_ratio:
            parts.append(f"--ratio {aspect_ratio}")
        text = " ".join(p for p in parts if p).strip()
        yielded_any = False
        async for result in self._run_generate(
            event, text, require_image=True, force_edit=True
        ):
            yield result
            yielded_any = True
        if not yielded_any:
            yield event.chain_result([Plain("图片编辑任务已提交，结果将直接发送到当前会话。")])
