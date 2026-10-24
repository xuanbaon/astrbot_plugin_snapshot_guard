"""快照守卫 —— 崩溃前留一手，崩了能回档。

AstrBot 自带「备份/导入」是手动的全量包；这个插件补两件它没有的事：
定时轻量快照（只收配置与插件状态，跳过图片素材），以及启动时的崩溃检测。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
import zipfile
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.message_event_result import MessageChain

PLUGIN_NAME = "astrbot_plugin_snapshot_guard"

# 只写和数据目录无关的通用名字，不假定用户装了哪些插件。
DEFAULT_EXCLUDES = [
    "attachments",
    "logs",
    "backups",
    "temp",
    "knowledge_base",
    "webchat",
    "plugins",
    # 主库在运行中被打开，直接覆盖 .db/.db-wal/.db-shm 会造成
    # “database disk image is malformed”。需要整库请用 WebUI 的备份功能。
    "data_v4.db",
    "plugin_data/astrbot_plugin_snapshot_guard/snapshots",
]

# 素材型目录用通配符排除，任何插件下的表情包/图片库都不会被卷进快照。
DEFAULT_GLOBS = [
    "*/packs/*",
    "*/images/*",
    "*/media/*",
    "*/downloads/*",
    "*_images/*",
]


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


class SnapshotGuard(Star):
    """定时给 AstrBot 的配置与插件状态拍快照，崩溃后一条命令回档。"""

    def __init__(self, context: Context, config: Any = None) -> None:
        super().__init__(context, config)
        self.config = config or {}
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            self.data_root: Path = Path(get_astrbot_data_path())
        except Exception:
            self.data_root = Path(__file__).resolve().parents[2]
        try:
            self.base_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            self.base_dir = self.data_root / "plugin_data" / PLUGIN_NAME
        self.snap_dir = self.base_dir / "snapshots"
        self.snap_dir.mkdir(parents=True, exist_ok=True)
        self.runtime_path = self.base_dir / "runtime.json"
        self._task: asyncio.Task | None = None
        self._last_snapshot_at = self._latest_snapshot_time()
        self._started_at = time.time()
        # 热重载时 on_astrbot_loaded 不会触发，这里兜底把心跳起来。
        self._write_runtime(False)
        self._start_loop()

    # ---------------- 配置 ----------------
    def _cfg(self, key: str, default: Any) -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            value = default
        return default if value is None else value

    @property
    def _excludes(self) -> list[str]:
        raw = self._cfg("excludes", DEFAULT_EXCLUDES) or []
        items = [str(item).strip("/\\").replace("\\", "/") for item in raw if str(item).strip()]
        if self._cfg("include_plugin_code", False):
            items = [item for item in items if item != "plugins"]
        return items

    @property
    def _globs(self) -> list[str]:
        raw = self._cfg("exclude_globs", DEFAULT_GLOBS) or []
        return [str(item).strip() for item in raw if str(item).strip()]

    @property
    def _max_file_mb(self) -> float:
        try:
            return max(1.0, float(self._cfg("max_file_mb", 20)))
        except Exception:
            return 20.0

    # ---------------- 快照 ----------------
    def _excluded(self, rel: str) -> bool:
        rel = rel.replace("\\", "/")
        # 任何 SQLite 的 WAL/SHM 都不进快照：它们和 .db 必须成套还原才有意义。
        if rel.endswith(".db-wal") or rel.endswith(".db-shm"):
            return True
        for item in self._excludes:
            if rel == item or rel.startswith(item + "/"):
                return True
        return any(fnmatch(rel, pattern) for pattern in self._globs)

    def create_snapshot(self, note: str = "") -> dict:
        target = self.snap_dir / f"snap-{_ts()}.zip"
        limit = int(self._max_file_mb * 1024 * 1024)
        files = 0
        skipped = 0
        total = 0
        manifest = {
            "created_at": time.time(),
            "note": note,
            "data_root": str(self.data_root),
            "excludes": self._excludes,
            "exclude_globs": self._globs,
            "files": 0,
            "bytes": 0,
        }
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in self.data_root.rglob("*"):
                if not path.is_file():
                    continue
                rel = path.relative_to(self.data_root).as_posix()
                if self._excluded(rel):
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                if size > limit:
                    skipped += 1
                    continue
                try:
                    zf.write(path, rel)
                except Exception:
                    skipped += 1
                    continue
                files += 1
                total += size
            manifest["files"] = files
            manifest["bytes"] = total
            manifest["skipped_large"] = skipped
            zf.writestr("_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        self._last_snapshot_at = time.time()
        self._prune()
        logger.info(f"[snapshot_guard] 已生成快照 {target.name}（{files} 个文件，{_human(total)}）")
        return {
            "path": str(target),
            "name": target.name,
            "files": files,
            "bytes": total,
            "skipped": skipped,
        }

    def _list_snapshots(self) -> list[dict]:
        rows: list[dict] = []
        for path in sorted(self.snap_dir.glob("snap-*.zip"), reverse=True):
            try:
                stat = path.stat()
            except OSError:
                continue
            note = ""
            try:
                with zipfile.ZipFile(path) as zf:
                    if "_manifest.json" in zf.namelist():
                        note = json.loads(zf.read("_manifest.json")).get("note", "")
            except Exception:
                note = ""
            rows.append(
                {
                    "name": path.name,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "note": note,
                }
            )
        return rows

    def _latest_snapshot_time(self) -> float:
        try:
            latest = max(
                (p.stat().st_mtime for p in self.snap_dir.glob("snap-*.zip")),
                default=0.0,
            )
            return latest
        except Exception:
            return 0.0

    def _prune(self) -> None:
        try:
            keep = max(1, int(self._cfg("keep_snapshots", 8)))
        except Exception:
            keep = 8
        for path in sorted(self.snap_dir.glob("snap-*.zip"), reverse=True)[keep:]:
            try:
                path.unlink()
            except Exception:
                pass

    def _restore(self, name: str) -> dict:
        key = str(name or "").strip()
        candidates = sorted(self.snap_dir.glob("snap-*.zip"), reverse=True)
        if not candidates:
            return {"ok": False, "message": "还没有任何快照"}
        if key in {"", "last", "最新"}:
            source = candidates[0]
        else:
            matches = [p for p in candidates if key in p.name]
            if not matches:
                return {"ok": False, "message": f"找不到快照 {key}"}
            source = matches[0]
        safety = self.create_snapshot(note=f"回档前自动备份（{source.name}）")
        restored = 0
        skipped_db = 0
        with zipfile.ZipFile(source) as zf:
            for info in zf.infolist():
                rel = info.filename
                if rel.startswith("/") or ".." in rel or rel == "_manifest.json":
                    continue
                # 硬保护：绝不在运行中覆盖 SQLite 主库与 WAL，否则会直接写坏库。
                if rel.endswith((".db", ".db-wal", ".db-shm")):
                    skipped_db += 1
                    continue
                destination = self.data_root / rel
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, destination.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    restored += 1
                except Exception:
                    continue
        message = f"已回档到 {source.name}，{restored} 个文件；回档前状态另存为 {safety['name']}"
        if skipped_db:
            message += f"。跳过 {skipped_db} 个数据库文件（运行中覆盖会写坏库，整库恢复请用 WebUI 备份）"
        return {
            "ok": True,
            "message": message,
            "restored": restored,
            "skipped_db": skipped_db,
            "source": source.name,
            "safety": safety["name"],
        }

    # ---------------- 崩溃检测 ----------------
    def _read_runtime(self) -> dict:
        try:
            return json.loads(self.runtime_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _write_runtime(self, clean: bool) -> None:
        payload = {
            "pid": os.getpid(),
            "started_at": self._started_at,
            "last_beat": time.time(),
            "clean": clean,
        }
        try:
            self.runtime_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _detect_crash(self) -> dict:
        previous = self._read_runtime()
        if not previous:
            return {}
        if previous.get("clean") is True:
            return {}
        if int(previous.get("started_at", 0)) >= int(self._started_at):
            return {}
        return {
            "previous_started_at": previous.get("started_at"),
            "previous_last_beat": previous.get("last_beat"),
            "pid": previous.get("pid"),
        }

    # ---------------- 主循环 ----------------
    async def _loop(self) -> None:
        beat_interval = max(10, int(self._cfg("heartbeat_seconds", 60) or 60))
        auto_hours = float(self._cfg("auto_interval_hours", 6) or 6)
        while True:
            try:
                self._write_runtime(False)
                if auto_hours > 0 and time.time() - self._last_snapshot_at >= auto_hours * 3600:
                    await asyncio.to_thread(self.create_snapshot, "定时快照")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("[snapshot_guard] 循环异常", exc_info=True)
            await asyncio.sleep(beat_interval)

    @filter.on_astrbot_loaded()
    async def on_loaded(self) -> None:
        self._boot()

    def _boot(self) -> None:
        crash = self._detect_crash()
        self._write_runtime(False)
        self._start_loop()
        if crash:
            stamp = datetime.fromtimestamp(float(crash.get("previous_last_beat") or 0)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            logger.warning(f"[snapshot_guard] 检测到上次运行非正常退出，最后心跳 {stamp}")
            self._crash_note = f"上次运行异常退出，最后心跳 {stamp}。可用 /快照 回档 恢复。"
            try:
                asyncio.get_running_loop().create_task(self._notify_admin(self._crash_note))
            except Exception:
                pass
        elif self._cfg("snapshot_on_start", True):
            try:
                asyncio.get_running_loop().create_task(asyncio.to_thread(self.create_snapshot, "启动快照"))
            except Exception:
                pass

    _crash_note: str = ""

    def _start_loop(self) -> None:
        if self._task and not self._task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._task = loop.create_task(self._loop())
        logger.info("[snapshot_guard] 已启动心跳与定时快照")

    async def _notify_admin(self, text: str) -> None:
        if not self._cfg("notify_on_crash", True):
            return
        target = str(self._cfg("admin_session", "") or "").strip()
        if not target or ":" not in target:
            return
        try:
            await self.context.send_message(target, MessageChain([Comp.Plain(f"【快照守卫】{text}")]))
        except Exception:
            logger.debug("[snapshot_guard] 崩溃通知发送失败", exc_info=True)

    # ---------------- 命令 ----------------
    @filter.command("快照")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def cmd_snapshot(self, event: AstrMessageEvent, sub: str = "", arg: str = ""):
        """快照：/快照 | /快照 列表 | /快照 回档 last | /快照 状态"""
        action = (sub or "").strip()
        if action in {"列表", "list"}:
            yield event.plain_result(self._render_list())
        elif action in {"回档", "restore"}:
            result = await asyncio.to_thread(self._restore, arg)
            yield event.plain_result(result["message"])
        elif action in {"状态", "status"}:
            yield event.plain_result(self._render_status())
        else:
            result = await asyncio.to_thread(self.create_snapshot, action or "手动快照")
            yield event.plain_result(
                f"已生成 {result['name']}：{result['files']} 个文件 / {_human(result['bytes'])}"
                f"（跳过超大文件 {result['skipped']} 个）"
            )

    def _render_list(self) -> str:
        rows = self._list_snapshots()
        if not rows:
            return "还没有快照。发 /快照 现在存一个。"
        lines = [f"共 {len(rows)} 个快照（新的在上）："]
        for row in rows[:10]:
            stamp = datetime.fromtimestamp(row["mtime"]).strftime("%m-%d %H:%M")
            lines.append(f"· {row['name']}　{stamp}　{_human(row['size'])}　{row['note']}")
        lines.append("回档：/快照 回档 last")
        return "\n".join(lines)

    def _render_status(self) -> str:
        latest = max((row["mtime"] for row in self._list_snapshots()), default=0.0)
        latest_text = (
            datetime.fromtimestamp(latest).strftime("%Y-%m-%d %H:%M")
            if latest
            else "从未快照"
        )
        hours = float(self._cfg("auto_interval_hours", 6) or 6)
        lines = [
            f"最近快照：{latest_text}",
            f"自动快照：{'每 %g 小时' % hours if hours > 0 else '已关闭'}",
            f"保留份数：{self._cfg('keep_snapshots', 8)}",
            f"排除项：{len(self._excludes)} 条路径 + {len(self._globs)} 条通配",
            f"数据目录：{self.data_root}",
        ]
        if getattr(self, "_crash_note", ""):
            lines.append(f"上次退出：{self._crash_note}")
        lines.append("提示：全量备份/导入在 WebUI 的「备份」页，本插件只管轻量快照与崩溃自检。")
        return "\n".join(lines)

    async def terminate(self) -> None:
        self._write_runtime(True)
        if self._task and not self._task.done():
            self._task.cancel()
