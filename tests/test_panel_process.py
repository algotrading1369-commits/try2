"""Real subprocess stop handshake without opening a game or browser."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
import unittest
import yaml
from pathlib import Path

from ogame_bot.panel_service import PanelService

RECEIVER = """
import sys,time
from pathlib import Path
from ogame_bot.core.panel_control import register_control,unregister_control,panel_stop_requested
directory=Path(sys.argv[1])
token=register_control(directory)
try:
    deadline=time.monotonic()+10
    while not panel_stop_requested(directory/'panel_stop.json'):
        if time.monotonic()>deadline:
            raise RuntimeError('Stop deadline exceeded')
        time.sleep(0.05)
finally:
    unregister_control(directory,token)
"""

LIVE_RECEIVER = """
import sys,asyncio
from pathlib import Path
from unittest.mock import MagicMock,AsyncMock
sys.path.insert(0,str(Path.cwd()/'tests'))
from run_without_io import LogicPolicy
from ogame_bot.config import load_config
from ogame_bot.scheduler.runtime import BotRuntime
from ogame_bot.core.session import SessionState
from ogame_bot.core.locking import profile_lock
from ogame_bot.core.panel_control import register_control,unregister_control,panel_stop_requested
from ogame_bot.errors import BrowserClosed
asyncio.set_event_loop_policy(LogicPolicy())
config=load_config(Path(sys.argv[1]))
directory=config.scheduler.status_file.parent
session=MagicMock()
session.state=SessionState.READY
session.before_navigation=None
session.check=AsyncMock()
def check_stop():
    if panel_stop_requested(directory/'panel_stop.json'):
        raise BrowserClosed('Stopped')
session._check_open=check_stop
with profile_lock(config.profile_dir):
    token=register_control(directory)
    try:
        async def main():
            runtime=BotRuntime(config,session,None,None,mode='session')
            try:
                await runtime.run()
            except BrowserClosed:
                pass
        asyncio.run(main())
    finally:
        unregister_control(directory,token)
"""


class ProcessControlTests(unittest.TestCase):
    def test_active_bot_consumes_instructions_and_acknowledges_without_restart(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "config.yaml"
            raw = yaml.safe_load((root / "config.example.yaml").read_bytes())
            for section in ("builder", "research", "fleet", "defender"):
                raw[section]["enabled"] = False
            config_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
            service = PanelService(config_path)
            child = subprocess.Popen([sys.executable, "-c", LIVE_RECEIVER, str(config_path)], cwd=root,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not service.process_status()["can_stop"]:
                    if child.poll() is not None or time.monotonic() > deadline:
                        self.fail("The live instruction receiver did not start.")
                    time.sleep(0.05)
                original_pid = service.process_status()["pid"]
                payload = service.settings()
                payload["builder"]["reserve"]["metal"] = 456
                result = service.save_settings(payload)
                expected = result["settings"]["version"]
                deadline = time.monotonic() + 5
                while True:
                    status = service.instruction_status()
                    if status.get("state") == "applied" and status.get("version") == expected:
                        break
                    if child.poll() is not None or time.monotonic() > deadline:
                        self.fail("The active bot did not acknowledge the new instructions.")
                    time.sleep(0.05)
                self.assertEqual(service.process_status()["pid"], original_pid)
                service.stop()
                child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0)
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.communicate(timeout=5)

    def test_reopened_panel_stops_registered_receiver_without_os_signal(self) -> None:
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            config = folder / "config.yaml"
            config.write_bytes((root / "config.example.yaml").read_bytes())
            service = PanelService(config)
            directory = service.config.scheduler.status_file.parent
            child = subprocess.Popen([sys.executable, "-c", RECEIVER, str(directory)], cwd=root,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not service.process_status()["can_stop"]:
                    if child.poll() is not None or time.monotonic() > deadline:
                        self.fail("The isolated receiver did not register control.")
                    time.sleep(0.05)
                reopened = PanelService(config)
                reopened.stop()
                child.communicate(timeout=5)
                self.assertEqual(child.returncode, 0)
                self.assertFalse(reopened.process_status()["can_stop"])
                self.assertFalse((directory / "bot_control.json").exists())
            finally:
                if child.poll() is None:
                    child.terminate()
                    child.communicate(timeout=5)
