"""绑定关系持久化:QQ号 → 角色标识。

存储位置:StarTools.get_data_dir()(AstrBot data/plugin_data/<插件名>/),
插件目录本身会被更新覆盖,数据不能放插件目录里。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path


class BindingStore:
    def __init__(self, data_dir: Path):
        self._path = Path(data_dir) / "bindings.json"
        self._lock = asyncio.Lock()
        self._data: dict[str, dict] = {}
        self._loaded = False

    def _load(self) -> None:
        if self._loaded:
            return
        if self._path.exists():
            try:
                self._data = json.loads(self._path.read_text("utf-8"))
            except (json.JSONDecodeError, OSError):
                # 文件损坏时不炸,当成空表(旧文件留着不覆盖)
                self._data = {}
        self._loaded = True

    async def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(self._path)

    async def set(self, user_id: str, identity: str, extra: dict | None = None) -> None:
        async with self._lock:
            self._load()
            self._data[user_id] = {"identity": identity, "extra": extra or {}}
            await self._save()

    async def get(self, user_id: str) -> dict | None:
        async with self._lock:
            self._load()
            return self._data.get(user_id)

    async def remove(self, user_id: str) -> bool:
        async with self._lock:
            self._load()
            if user_id in self._data:
                del self._data[user_id]
                await self._save()
                return True
            return False

    async def all(self) -> dict[str, dict]:
        async with self._lock:
            self._load()
            return dict(self._data)

    async def prune_identities(self, keep: set[str]) -> list[str]:
        """删掉所绑角色已不在 keep 里的记录,返回被清理的用户 id。

        用于「管理员删掉某个角色的凭据后,对应用户的绑定一并失效」。
        """
        async with self._lock:
            self._load()
            removed = [
                uid
                for uid, item in self._data.items()
                if str(item.get("identity") or "").strip() not in keep
            ]
            if removed:
                for uid in removed:
                    del self._data[uid]
                await self._save()
            return removed
