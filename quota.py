"""按用户按日的生图次数配额。"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

try:
    from astrbot.api import logger
except Exception:
    import logging

    logger = logging.getLogger(__name__)


# 东八区自然日（与国内使用场景一致）
_TZ_CN = timezone(timedelta(hours=8))


def today_key() -> str:
    return datetime.now(_TZ_CN).strftime("%Y-%m-%d")


class DailyQuota:
    """
    持久化每日调用次数。
    结构：
    {
      "date": "2026-07-14",
      "users": { "user_id": count, ... }
    }
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"date": today_key(), "users": {}}
        self._load()

    def _load(self) -> None:
        try:
            if self.path.is_file():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self._data = {
                        "date": str(raw.get("date") or today_key()),
                        "users": dict(raw.get("users") or {}),
                    }
        except Exception:
            self._data = {"date": today_key(), "users": {}}
        self._roll_if_needed()

    def _roll_if_needed(self) -> None:
        today = today_key()
        if self._data.get("date") != today:
            self._data = {"date": today, "users": {}}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except Exception as e:
            logger.warning(
                f"[gpt_image] quota persist failed (in-memory only): {e}"
            )

    def get_used(self, user_id: str) -> int:
        with self._lock:
            self._roll_if_needed()
            uid = str(user_id or "").strip() or "unknown"
            try:
                return max(0, int(self._data["users"].get(uid, 0)))
            except Exception:
                return 0

    def remaining(self, user_id: str, limit: int) -> int:
        if limit < 0:
            return -1  # unlimited
        used = self.get_used(user_id)
        return max(0, int(limit) - used)

    def can_use(self, user_id: str, limit: int) -> tuple[bool, int, int]:
        """
        返回 (是否可用, 已用, 限额)。
        limit < 0 表示无限。
        """
        if limit < 0:
            return True, self.get_used(user_id), -1
        used = self.get_used(user_id)
        return used < int(limit), used, int(limit)

    def consume(self, user_id: str, n: int = 1) -> int:
        """Increment usage by n. Returns new used count. (legacy: post-deduct)"""
        with self._lock:
            self._roll_if_needed()
            uid = str(user_id or "").strip() or "unknown"
            try:
                cur = int(self._data["users"].get(uid, 0))
            except Exception:
                cur = 0
            cur = max(0, cur) + max(1, int(n))
            self._data["users"][uid] = cur
            self._save()
            return cur

    def reserve(self, user_id: str, limit: int) -> tuple[bool, int]:
        """Atomically check + pre-deduct 1 from the daily quota.

        Returns (ok, new_used_count).
        limit < 0 means unlimited (always ok, no deduction).
        On upstream failure, caller should call refund().
        """
        if limit < 0:
            return True, self.get_used(user_id)
        with self._lock:
            self._roll_if_needed()
            uid = str(user_id or "").strip() or "unknown"
            try:
                cur = int(self._data["users"].get(uid, 0))
            except Exception:
                cur = 0
            cur = max(0, cur)
            if cur >= int(limit):
                return False, cur
            cur += 1
            self._data["users"][uid] = cur
            self._save()
            return True, cur

    def refund(self, user_id: str, n: int = 1) -> int:
        """Refund n reservations (e.g. upstream generation failed)."""
        with self._lock:
            self._roll_if_needed()
            uid = str(user_id or "").strip() or "unknown"
            try:
                cur = int(self._data["users"].get(uid, 0))
            except Exception:
                cur = 0
            cur = max(0, cur - max(1, int(n)))
            self._data["users"][uid] = cur
            self._save()
            return cur

    def reset_user(self, user_id: str) -> None:
        with self._lock:
            self._roll_if_needed()
            uid = str(user_id or "").strip()
            if uid and uid in self._data["users"]:
                del self._data["users"][uid]
                self._save()

    def stats_summary(self) -> dict[str, Any]:
        with self._lock:
            self._roll_if_needed()
            return {
                "date": self._data.get("date"),
                "user_count": len(self._data.get("users") or {}),
                "total_calls": sum(int(v or 0) for v in (self._data.get("users") or {}).values()),
            }
