"""Publish Unifideck Xbox PC achievements for the Nexus Achievements plugin."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from unifideck.launcher.game_title import registered_title
from unifideck.launcher.xbox_pc.record_lookup import find_install_record
from unifideck.launcher.xbox_pc.layout import data_root, ensure_private_dir, read_json_dict, write_private_json
from unifideck.services.artwork.store_metadata import microsoft_metadata
from unifideck.stores.microsoft.achievements import hex_title_id

logger = logging.getLogger(__name__)


class NexusFeed:
    """Export snapshots and new unlocks from the existing Xbox watcher."""

    def __init__(self, game_id: str, title_id: int, product_id: str = "") -> None:
        self.game_id = game_id
        self.title_id = f"unifideck.{title_id}"
        self.has_baseline = False
        self.product_id = product_id
        self.cover_url = ""

    @classmethod
    def for_game(cls, game_id: str) -> NexusFeed | None:
        record = find_install_record(game_id)
        title_id = hex_title_id(record.get("titleId")) if record and record.get("state") == "installed" else None
        return cls(game_id, title_id, record["productId"]) if title_id else None

    @property
    def directory(self) -> Path:
        return data_root() / "achievements"

    async def load_cover(self) -> None:
        """Reuse the portrait from a previous snapshot or resolve the product's art."""
        previous = read_json_dict(self.directory / f"{self.title_id}.json") or {}
        self.cover_url = previous.get("coverUrl") or ""
        if not self.cover_url and self.product_id:
            metadata = await microsoft_metadata(self.product_id, timeout=5)
            self.cover_url = metadata.get("urls", {}).get("grid", "")

    def _name(self) -> str:
        return registered_title(f"microsoft:{self.game_id}")[:256] or self.game_id

    def set_running(self, running: bool) -> None:
        path = self.directory / "running.json"
        try:
            if running:
                ensure_private_dir(path.parent)
                write_private_json(path, {"titleId": self.title_id, "launcherPid": os.getpid()})
            elif json.loads(path.read_text()).get("titleId") == self.title_id:
                path.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError):
            logger.debug("Could not update Nexus Achievements running title", exc_info=True)

    def publish(self, payload: dict[str, Any], new_keys: set[str]) -> None:
        """Save the latest list before announcing its newly unlocked rows."""
        title_name = self._name()
        now = int(time.time() * 1000)
        items = {}
        for row in payload["achievements"][:500]:
            timestamp = row.get("unlocked_at")
            items[row["key"]] = {
                "id": row["key"][:180],
                "name": row["name"][:256],
                "description": row["description"][:1500],
                "iconUrl": row["image_unlocked"],
                "gamerscore": row["gamerscore"] or 0,
                "unlocked": row["unlocked"],
                "unlockedAt": int(timestamp * 1000) if timestamp is not None else None,
                "secret": row["hidden"],
                "progress": row.get("progress"),
            }
        unlocked = [item for item in items.values() if item["unlocked"]]
        snapshot = {
            "titleId": self.title_id, "titleName": title_name, "coverUrl": self.cover_url,
            "updatedAt": now, "unlocked": len(unlocked), "total": len(items),
            "gamerscore": sum(item["gamerscore"] for item in unlocked),
            "gamerscoreTotal": sum(item["gamerscore"] for item in items.values()),
            "achievements": list(items.values()),
        }
        try:
            ensure_private_dir(self.directory)
            write_private_json(self.directory / f"{self.title_id}.json", snapshot)
            events = [items[key] for key in items if key in new_keys] if self.has_baseline else []
            self.has_baseline = True
            if events:
                self._append_events(events, title_name, now)
        except OSError:
            logger.debug("Could not publish Nexus Achievements data", exc_info=True)

    def _append_events(self, items: list[dict[str, Any]], title_name: str, now: int) -> None:
        path = self.directory / "achievement-events.jsonl"
        if path.exists() and path.stat().st_size >= 1_000_000:
            path.replace(path.with_suffix(".jsonl.1"))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            for item in items:
                event = {**item, "type": "achievement", "ts": now,
                         "titleId": self.title_id, "titleName": title_name}
                handle.write(json.dumps(event) + "\n")
