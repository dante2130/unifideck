"""The Microsoft store's achievements methods.

A mixin so ``microsoft_store.py`` stays under the file-size cap. It gives
the store the two methods the achievements RPC and watcher call on every
achievement store: ``get_game_achievements`` and ``invalidate_achievements``.
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from unifideck.stores.shared.achievements_payload import TtlCache, build_payload
from unifideck.stores.shared.timestamps import parse_timestamp
from unifideck.utils.locale import get_unifideck_locale

from .achievements import MicrosoftAchievements, MicrosoftAchievementsError, hex_title_id
from .xodus.cli import XodusCliError
from .xodus.ids import record_for_game

if TYPE_CHECKING:
    from unifideck.config import ConfigManager

    from .tokens import MicrosoftTokenManager
    from .xodus.xbox_pc import XboxPc

#: Used until an xCloud session has told us the account's market. Title ids
#: are the same in every market; this only picks which catalog to ask.
_FALLBACK_MARKET = "US"


class MicrosoftAchievementsMixin:
    """Xbox achievements for :class:`MicrosoftStore` (display + watcher)."""

    _tokens: MicrosoftTokenManager
    _last_market: str | None
    _config_manager: ConfigManager | None
    _achievements: MicrosoftAchievements | None = None
    _pc_achievement_cache: TtlCache | None = None
    _xbox_pc: XboxPc

    def _achievements_reader(self) -> MicrosoftAchievements:
        if self._achievements is None:
            self._achievements = MicrosoftAchievements(
                self._tokens,
                market_fn=lambda: self._last_market or _FALLBACK_MARKET,
                locale_fn=lambda: get_unifideck_locale(self._config_manager),
            )
        return self._achievements

    async def get_game_achievements(
        self, game_id: str, force: bool = False,
    ) -> dict[str, Any]:
        """An Xbox game's achievements (definitions + this user's unlocks).

        Raises ``MicrosoftAchievementsError`` on auth/network failure or
        when the game has no Xbox title id; a title with no achievements is
        a normal empty payload. ``force`` bypasses the TTL cache.
        """
        record = await asyncio.to_thread(record_for_game, game_id)
        title_id = hex_title_id(record.get("titleId")) if record and record.get("state") == "installed" else None
        if title_id and self._xbox_pc.enabled():
            return await self._get_pc_achievements(game_id, title_id, force)
        return await self._achievements_reader().get_game_achievements(
            game_id, force=force,
        )

    async def _get_pc_achievements(self, game_id: str, title_id: int, force: bool) -> dict[str, Any]:
        if self._pc_achievement_cache is None:
            self._pc_achievement_cache = TtlCache()
        cache = self._pc_achievement_cache
        if not force and (cached := cache.get(game_id)) is not None:
            return cached
        try:
            raw = await self._xbox_pc.cli.achievements(title_id)
        except XodusCliError as error:
            # Older backends lack this command. Preserve the Microsoft store
            # reader for users whose OAuth sign-in can fetch the same title.
            if error.code == "exit_2":
                return await self._achievements_reader().get_game_achievements(game_id, force=force)
            raise MicrosoftAchievementsError("offline") from error
        rows = []
        for item in raw["items"]:
            if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
                continue
            unlocked = item.get("state") == "unlocked" and not item.get("revoked")
            icon = item.get("iconUrl") or ""
            rows.append({
                "key": item["id"], "name": item["name"], "description": item.get("description") or "",
                "image_unlocked": icon, "image_locked": icon, "hidden": item.get("secret") is True,
                "unlocked": unlocked, "unlocked_at": parse_timestamp(item.get("unlockedAt")) if unlocked else None,
                "gamerscore": item.get("gamerscore"), "progress": None if unlocked else item.get("progress"),
                "rarity": (item.get("rarity") or {}).get("percentage"),
            })
        payload = build_payload("microsoft", game_id, rows)
        cache.put(game_id, payload)
        return payload

    def invalidate_achievements(self, game_id: str) -> None:
        """Drop a game's cached achievements."""
        if self._pc_achievement_cache is not None:
            self._pc_achievement_cache.invalidate(str(game_id))
        if self._achievements is not None:
            self._achievements.invalidate(game_id)
