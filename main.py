"""AstrBot 插件：通过 adobe2api 调用 Firefly GPT Image 生图。"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from pathlib import Path
from typing import Any, AsyncGenerator, Optional

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
    r"^[/!！.．]?(?:gpt改图|gpt编辑|gptedit|gedit|改图)(?=$|[\s,，:：]|[\u4e00-\u9fff])",
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
    "1.5.5",
)
class GptImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self.client = self._build_client()
        plugin_data = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_gpt_image"
        plugin_data.mkdir(parents=True, exist_ok=True)
        self.data_dir = plugin_data / "output"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.quota = DailyQuota(plugin_data / "daily_quota.json")
        self._rebuild_policies()

    def _rebuild_policies(self) -> None:
        """Build URL/path policies + size limits from config."""
        napcat_hosts = str(
            self._cfg("napcat_hosts", "127.0.0.1") or "127.0.0.1"
        )
        image_suffixes = str(
            self._cfg(
                "image_host_suffixes",
                "qpic.cn qq.com myqcloud.com gtimg.cn",
            )
            or "qpic.cn qq.com myqcloud.com gtimg.cn"
        )
        self._url_policy = UrlPolicy.from_config(
            napcat_hosts=napcat_hosts,
            image_host_suffixes=image_suffixes,
            allow_public_http=bool(self._cfg("allow_public_http", False)),
        )
        self._path_policy = PathPolicy.from_config(
            allowed_media_dirs=str(self._cfg("allowed_media_dirs", "") or ""),
        )
        try:
            self._max_single_image_bytes = int(
                self._cfg("max_single_image_bytes", DEFAULT_MAX_SINGLE_IMAGE_BYTES)
                or DEFAULT_MAX_SINGLE_IMAGE_BYTES
            )
        except Exception:
            self._max_single_image_bytes = DEFAULT_MAX_SINGLE_IMAGE_BYTES
        try:
            self._max_output_bytes = int(
                self._cfg("max_output_bytes", DEFAULT_MAX_OUTPUT_BYTES)
                or DEFAULT_MAX_OUTPUT_BYTES
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
        return Adobe2APIClient(
            base_url=str(self._cfg("base_url", "") or ""),
            api_key=str(self._cfg("api_key", "") or ""),
            timeout=float(self._cfg("request_timeout", 300) or 300),
            max_retries=int(self._cfg("max_retries", 2) or 0),
            retry_backoff=float(self._cfg("retry_backoff", 3) or 3),
        )

    def _reload_client_if_needed(self) -> None:
        self._rebuild_policies()
        new = self._build_client()
        old = self.client
        changed = (
            new.base_url != old.base_url
            or new.api_key != old.api_key
            or abs(new.timeout - old.timeout) > 1e-6
            or new.max_retries != old.max_retries
            or abs(new.retry_backoff - old.retry_backoff) > 1e-6
        )
        if not changed:
            return
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
        limit = self._daily_limit_value()
        if self.client.configured():
            logger.info(
                f"GPT Image 插件已加载：{self.client.base_url} "
                f"(daily_limit={limit}, retries={self.client.max_retries})"
            )
        else:
            logger.warning("GPT Image 插件未配置 base_url，请在 WebUI 插件配置中填写")

    async def terminate(self):
        await self.client.close()

    # ------------------------------------------------------------------
    # permission / admin / quota
    # ------------------------------------------------------------------

    def _whitelist_ids(self) -> set[str]:
        raw = str(self._cfg("allowed_users", "") or "")
        parts = re.split(r"[\s,;，；]+", raw)
        return {p.strip() for p in parts if p.strip()}

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
        mode = str(self._cfg("permission_mode", "all") or "all").lower()
        if mode in ("", "all", "everyone", "public"):
            return True, ""

        sender = self._user_id(event)
        is_admin = self._is_admin(event)

        if mode in ("admin", "admins", "管理员"):
            if is_admin:
                return True, ""
            return False, "⛔ 此指令仅 AstrBot 全局管理员可用。"

        if mode in ("whitelist", "wl", "白名单"):
            if is_admin:
                return True, ""
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
        去掉指令前缀。支持 `/gpt图给她换装`（中文指令后无空格）。
        """
        text = self._raw_message_text(event)
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
            # gpt图 后可直接接中文，\s* 允许零空格
            text = re.sub(
                r"^[/!！.．]?gpt图\s*",
                "",
                text,
                count=1,
                flags=re.IGNORECASE,
            )
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
            path_policy=self._path_policy,
            max_single_bytes=self._max_single_image_bytes,
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
            self._cfg("audit_failure_policy", "keyword_only") or "keyword_only"
        ).strip().lower()
        if audit_failure_policy not in ("block", "keyword_only", "allow"):
            audit_failure_policy = "keyword_only"

        audit_system_prompt = str(self._cfg("audit_prompt", "") or "").strip()
        audit_provider_id = str(self._cfg("audit_provider_id", "") or "").strip()
        analyze_kwargs = dict(
            umo=getattr(event, "unified_msg_origin", None),
            strict=bool(self._cfg("audit_strict", True)),
            timeout=float(self._cfg("llm_timeout", 45) or 45),
            system_prompt=audit_system_prompt or None,
            enable_keyword_filter=bool(self._cfg("enable_keyword_filter", True)),
            provider_id=audit_provider_id or None,
            audit_failure_policy=audit_failure_policy,
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

        # Aspect ratio post-processing
        if manual_ratio:
            analyze.aspect_ratio = manual_ratio
            if "manual" not in analyze.source:
                analyze.source = f"{analyze.source}+manual"
        elif not auto_aspect:
            # auto_aspect off: use default ratio (LLM ratio ignored)
            analyze.aspect_ratio = default_ratio
        elif ref_images:
            # 改图：默认按参考图原始比例。用第一张即可；多图时以主图为准。
            ref_ratio = None
            for ref in ref_images:
                size = probe_image_size(ref)
                if size:
                    w, h = size
                    ref_ratio = nearest_ratio(w, h)
                    logger.info(
                        f"[gpt_image] 参考图尺寸 {w}x{h} → 使用比例 {ref_ratio}"
                    )
                    break
            if ref_ratio:
                analyze.aspect_ratio = ref_ratio
                if "ref" not in analyze.source:
                    analyze.source = f"{analyze.source}+ref"

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
        self._reload_client_if_needed()

        ok, deny = self._check_permission(event)
        if not ok:
            yield self._quoted_plain(event, deny)
            return

        if not self.client.configured():
            yield self._quoted_plain(
                event,
                "⚠️ 未配置 adobe2api 地址。请在插件配置中填写 base_url 与 api_key。",
            )
            return

        text = (raw_text or "").strip()
        if not text or text in {"help", "帮助", "?", "？"}:
            yield self._quoted_plain(event, HELP_TEXT)
            return

        # 先收参考图（改图指令必须有图）
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
            # 用户附图了但提取失败：不要默默改走文生图
            yield self._quoted_plain(
                event,
                "消息里好像有图，但没能读到参考图，已取消（避免变成纯文生图）。\n"
                "请重新发送原图后再试 /gpt改图 或 /gpt图。",
            )
            return

        # Atomic quota reservation (replaces check-then-consume)
        limit = self._quota_limit_for(event)
        reserved = False
        if limit >= 0:
            ok_q, _ = self.quota.reserve(self._user_id(event), limit)
            if not ok_q:
                yield self._quoted_plain(
                    event, "今日次数已用完，请明天再试。"
                )
                return
            reserved = True

        # Progress notification via send (avoids recall plugins)
        await self._notify(
            event,
            f"⏳ 正在{'改图' if is_edit else '生图'}…（已取到参考图×{len(ref_images)}）"
            if ref_images
            else "⏳ 正在生图…",
        )

        analyze, prompt, model_id, err = await self._analyze_and_build(
            event, text, ref_images=ref_images or None
        )
        if err:
            if reserved:
                self.quota.refund(self._user_id(event))
            yield self._quoted_plain(event, err)
            return
        if analyze is not None and not analyze.allowed:
            if reserved:
                self.quota.refund(self._user_id(event))
            logger.info(
                f"[gpt_image] request blocked user={self._user_id(event)} "
                f"reason={analyze.reason}"
            )
            yield self._quoted_plain(event, "暂时无法处理该请求，请换一个描述再试。")
            return
        if not model_id or not prompt:
            if reserved:
                self.quota.refund(self._user_id(event))
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

        try:
            result = await self.client.generate_image(
                prompt=prompt,
                model=model_id,
                image_data_urls=ref_images or None,
                timeout=float(self._cfg("request_timeout", 300) or 300),
            )
        except Adobe2APIError as e:
            if reserved:
                self.quota.refund(self._user_id(event))
            logger.error(f"[gpt_image] generate failed: {e}")
            yield self._quoted_plain(event, self._format_user_error(e))
            return
        except Exception as e:
            if reserved:
                self.quota.refund(self._user_id(event))
            logger.exception("[gpt_image] unexpected error")
            yield self._quoted_plain(event, self._format_user_error(e))
            return

        logger.info(f"[gpt_image] adobe2api ok model={result.get('model')}")

        new_used = self.quota.get_used(self._user_id(event))

        footer_parts: list[str] = []
        if bool(self._cfg("show_meta", True)) and analyze:
            footer_parts.append(
                f"{analyze.resolution.upper()} · {analyze.aspect_ratio} · {mode}"
            )
        if limit >= 0:
            remain = max(0, limit - new_used)
            footer_parts.append(f"今日还可生成 {remain} 次（已用 {new_used}/{limit}）")
        footer = "\n".join(footer_parts)

        await self._cleanup_old_files()
        stem = f"gpt_image_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
        try:
            path = await self.client.save_result_image(
                result, self.data_dir, stem, max_bytes=self._max_output_bytes
            )
        except Exception as e:
            logger.warning(f"[gpt_image] save failed, trying direct URL: {e}")
            if result.get("url"):
                chain = []
                if footer:
                    chain.append(Plain(f"✅ 生成完成\n{footer}\n"))
                chain.append(Image.fromURL(str(result["url"])))
                quoted = self._with_user_quote(event, chain)
                try:
                    await event.send(MessageChain(chain=quoted))
                except Exception:
                    pass
                yield event.chain_result(quoted)
                return
            yield self._quoted_plain(event, "❌ 图片保存失败，请稍后再试。")
            return

        chain = []
        if footer:
            chain.append(Plain(f"✅ 生成完成\n{footer}\n"))
        chain.append(Image.fromFileSystem(str(path)))
        quoted = self._with_user_quote(event, chain)
        try:
            await event.send(MessageChain(chain=quoted))
            logger.info(f"[gpt_image] result sent path={redact_path(path)}")
        except Exception as e:
            logger.warning(f"[gpt_image] send failed, fallback yield: {e}")
            yield event.chain_result(quoted)
            return
        # 已 send，再给一个空/轻量结果避免框架报未返回
        # 不重复发图：只 yield 文本确认（若平台会去重也无所谓）
        return

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------
    #
    # 注意：AstrBot 标准 @filter.command 要求「指令名 + 空格」，
    # 中文常见写法 `/gpt图给她换装`（无空格）匹配失败，会落到主 Agent。
    # 因此用 regex 做主入口（不受 wake_prefix 制约，自行判断是否接管）。

    @filter.regex(
        r"(?is)^[/!！.．]?(?:gpt改图|gpt编辑|gptedit|gedit|改图|"
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
        async for result in self._run_generate(event, text):
            yield result

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
        async for result in self._run_generate(
            event, text, require_image=True, force_edit=True
        ):
            yield result
