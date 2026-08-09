from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

from .autostart import project_root
from .config import Settings
from .db import Database
from .notifications import build_notification_service
from .resources import bridge_directory

logger = logging.getLogger(__name__)


@dataclass
class RestartBackoff:
    maximum: int = 120
    current: int = 1

    def success(self) -> None:
        self.current = 1

    def fail(self) -> int:
        delay = self.current
        self.current = min(self.maximum, max(1, self.current * 2))
        return delay


def heartbeat_is_stale(age: float | None, stale_seconds: int, *, grace: bool = False) -> bool:
    if age is None:
        return not grace
    return age > stale_seconds


class RuntimeWatchdog:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.db = Database(settings.db_path)
        self.notifications = build_notification_service(settings, self.db)
        self.root = project_root()
        self.runtime: subprocess.Popen | None = None
        self.bridge: subprocess.Popen | None = None
        self.runtime_started_at = 0.0
        self.bridge_started_at = 0.0
        self.runtime_backoff = RestartBackoff(settings.watchdog_restart_backoff_max_seconds)
        self.bridge_backoff = RestartBackoff(settings.watchdog_restart_backoff_max_seconds)
        self._stopping = False
        self._runtime_log = None
        self._bridge_log = None

    @property
    def bridge_health_url(self) -> str:
        parts = urlsplit(self.settings.decision_bridge_url)
        return urlunsplit((parts.scheme, parts.netloc, "/health", "", ""))

    async def bridge_healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(self.bridge_health_url)
                if response.status_code != 200:
                    return False
                return bool(response.json().get("ok"))
        except Exception:
            return False

    def _open_logs(self) -> None:
        logs = self.root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        if self._runtime_log is None:
            self._runtime_log = open(logs / "runtime-supervised.log", "a", encoding="utf-8", buffering=1)
        if self._bridge_log is None:
            self._bridge_log = open(logs / "bridge-supervised.log", "a", encoding="utf-8", buffering=1)

    async def start_bridge(self) -> None:
        if await self.bridge_healthy():
            self.db.set_state("bridge_status", "healthy-external")
            self.bridge_backoff.success()
            return
        if not self.settings.bridge_auto_start:
            self.db.set_state("bridge_status", "down-autostart-disabled")
            return
        if self.settings.browser_mcp_dir is None:
            self.db.set_state("bridge_status", "down-browser-mcp-dir-missing")
            return
        node = shutil.which("node")
        try:
            bridge_dir = bridge_directory()
            bridge_script = bridge_dir / "server.mjs"
        except RuntimeError:
            self.db.set_state("bridge_status", "down-bridge-assets-missing")
            return
        if not node or not bridge_script.exists():
            self.db.set_state("bridge_status", "down-node-or-script-missing")
            return
        if self.bridge and self.bridge.poll() is None:
            return
        self._open_logs()
        env = os.environ.copy()
        env.setdefault("GPTTRADDER_BROWSER_MCP_DIR", str(self.settings.browser_mcp_dir))
        env.setdefault("GPTTRADDER_CHATGPT_SESSION_KEY", self.settings.chatgpt_session_key)
        if self.settings.chatgpt_conversation_url:
            env.setdefault("GPTTRADDER_CHATGPT_CONVERSATION_URL", self.settings.chatgpt_conversation_url)
        env.setdefault("GPTTRADDER_CHATGPT_TIMEOUT_SECONDS", str(self.settings.chatgpt_timeout_seconds))
        env.setdefault("CHATGPT_HEADLESS", "true" if self.settings.chatgpt_headless else "false")
        self.bridge = subprocess.Popen(
            [node, str(bridge_script)],
            cwd=bridge_dir,
            env=env,
            stdout=self._bridge_log,
            stderr=subprocess.STDOUT,
        )
        self.bridge_started_at = time.time()
        self.db.set_state("bridge_status", "starting")

    def start_runtime(self) -> None:
        if self.runtime and self.runtime.poll() is None:
            return
        self._open_logs()
        self.runtime = subprocess.Popen(
            [sys.executable, "-m", "gpttradder.cli", "run"],
            cwd=self.root,
            env=os.environ.copy(),
            stdout=self._runtime_log,
            stderr=subprocess.STDOUT,
        )
        self.runtime_started_at = time.time()
        self.db.set_state("watchdog_status", "runtime-starting")

    async def stop_process(self, process: subprocess.Popen | None, name: str) -> None:
        if process is None or process.poll() is not None:
            return
        logger.warning("Stopping %s pid=%s", name, process.pid)
        try:
            process.terminate()
            await asyncio.to_thread(process.wait, 10)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass

    async def run(self) -> None:
        self.db.set_state("watchdog_status", "starting")
        await self.notifications.runtime("Watchdog started.", key="watchdog-started")
        try:
            while not self._stopping:
                self.db.heartbeat("watchdog", {"pid": os.getpid()})

                bridge_ok = await self.bridge_healthy()
                if not bridge_ok:
                    if self.bridge and self.bridge.poll() is not None:
                        code = self.bridge.returncode
                        self.db.set_state("bridge_status", f"exited:{code}")
                        delay = self.bridge_backoff.fail()
                        await self.notifications.runtime(
                            f"ChatGPT bridge exited ({code}); restarting after {delay}s.",
                            severity="ERROR",
                            key="bridge-exited",
                        )
                        self.bridge = None
                        await asyncio.sleep(delay)
                    elif (
                        self.bridge
                        and self.bridge.poll() is None
                        and time.time() - self.bridge_started_at > self.settings.watchdog_stale_seconds
                    ):
                        # Process-alive is not enough: a wedged bridge that never becomes
                        # healthy must be recycled instead of surviving forever.
                        await self.notifications.runtime(
                            "ChatGPT bridge process is alive but health check stayed down; recycling it.",
                            severity="ERROR",
                            key="bridge-health-stale",
                        )
                        await self.stop_process(self.bridge, "bridge")
                        self.bridge = None
                        self.db.set_state("bridge_status", "recycling-unhealthy")
                        await asyncio.sleep(self.bridge_backoff.fail())
                    await self.start_bridge()
                    # Give a newly launched bridge a short window before the next probe.
                    if self.bridge and self.bridge.poll() is None and time.time() - self.bridge_started_at < 20:
                        await asyncio.sleep(min(2, self.settings.watchdog_poll_seconds))
                        continue
                else:
                    self.db.set_state("bridge_status", "healthy")
                    self.bridge_backoff.success()

                # The normal non-mock runtime requires a healthy bridge before startup.
                if bridge_ok:
                    if self.runtime is None or self.runtime.poll() is not None:
                        if self.runtime is not None:
                            code = self.runtime.returncode
                            delay = self.runtime_backoff.fail()
                            await self.notifications.runtime(
                                f"Trading runtime exited ({code}); restarting after {delay}s.",
                                severity="ERROR",
                                key="runtime-exited",
                            )
                            await asyncio.sleep(delay)
                        self.start_runtime()
                    else:
                        age = self.db.heartbeat_age("runtime")
                        in_grace = time.time() - self.runtime_started_at < self.settings.watchdog_stale_seconds
                        if heartbeat_is_stale(age, self.settings.watchdog_stale_seconds, grace=in_grace):
                            await self.notifications.runtime(
                                f"Runtime heartbeat stale ({age}); forcing restart.",
                                severity="ERROR",
                                key="runtime-heartbeat-stale",
                            )
                            await self.stop_process(self.runtime, "runtime")
                            self.runtime = None
                        else:
                            self.runtime_backoff.success()
                            self.db.set_state("watchdog_status", "healthy")

                await asyncio.sleep(self.settings.watchdog_poll_seconds)
        finally:
            self._stopping = True
            self.db.set_state("watchdog_status", "stopping")
            await self.stop_process(self.runtime, "runtime")
            if self.bridge is not None:
                await self.stop_process(self.bridge, "bridge")
            self.db.set_state("watchdog_status", "stopped")
            if self._runtime_log:
                self._runtime_log.close()
            if self._bridge_log:
                self._bridge_log.close()


def install_signal_handlers(loop: asyncio.AbstractEventLoop, watchdog: RuntimeWatchdog) -> None:
    def stop() -> None:
        watchdog._stopping = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop)
        except (NotImplementedError, RuntimeError):
            # Windows Proactor loops may not support add_signal_handler.
            pass


async def run_watchdog(settings: Settings) -> None:
    watchdog = RuntimeWatchdog(settings)
    install_signal_handlers(asyncio.get_running_loop(), watchdog)
    await watchdog.run()
