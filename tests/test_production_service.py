from __future__ import annotations

import os
import subprocess
import tomllib
import unittest
from pathlib import Path
from unittest.mock import Mock
from unittest.mock import patch

from bot.production_service import (
    API_COMMAND,
    CHILD_COMMANDS,
    Child,
    GRID_BOT_1_COMMAND,
    GRID_BOT_2_COMMAND,
    LIVE_RUNNER_COMMAND,
    MAX_CHILD_RESTARTS,
    _supervise_child,
    main,
    production_environment,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ProductionServiceTests(unittest.TestCase):
    def test_production_environment_preserves_managed_live_flag_and_defaults_api(self) -> None:
        with patch.dict(
            os.environ,
            {"ENABLE_LIVE_TRADING": "true"},
            clear=True,
        ):
            environment = production_environment()

        self.assertEqual(environment["ENABLE_LIVE_TRADING"], "true")
        self.assertEqual(environment["PORT"], "8080")
        self.assertEqual(environment["NODE_ENV"], "production")
        self.assertEqual(environment["TRADING_GRID_BOT_1_ENABLE_LIVE_TRADING"], "false")
        self.assertEqual(environment["TRADING_GRID_BOT_2_STRATEGY_MODE"], "disabled")

    def test_children_are_api_bridge_and_two_isolated_grid_services(self) -> None:
        self.assertEqual(
            API_COMMAND,
            ("node", "--enable-source-maps", "artifacts/api-server/dist/index.mjs"),
        )
        self.assertEqual(LIVE_RUNNER_COMMAND, GRID_BOT_1_COMMAND)
        self.assertEqual(GRID_BOT_1_COMMAND[-2:], ("-u", "grid_bot_1.py"))
        self.assertEqual(GRID_BOT_2_COMMAND[-2:], ("-u", "grid_bot_2.py"))
        self.assertEqual(tuple(role for role, _ in CHILD_COMMANDS), ("api", "grid_bot_1", "grid_bot_2"))

    def test_supervisor_starts_both_children_and_stops_them_on_shutdown(self) -> None:
        event = Mock()
        event.wait.return_value = True
        api_process = Mock(spec=subprocess.Popen)
        bot1_process = Mock(spec=subprocess.Popen)
        bot2_process = Mock(spec=subprocess.Popen)
        api_process.pid = 101
        bot1_process.pid = 102
        bot2_process.pid = 103
        api_process.poll.return_value = None
        bot1_process.poll.return_value = None
        bot2_process.poll.return_value = None

        with (
            patch("bot.production_service.Event", return_value=event),
            patch("bot.production_service.signal.signal"),
            patch(
                "bot.production_service.subprocess.Popen",
                side_effect=[api_process, bot1_process, bot2_process],
            ) as popen,
        ):
            self.assertEqual(main(), 0)

        self.assertEqual(popen.call_count, 3)
        self.assertEqual(popen.call_args_list[0].args[0], API_COMMAND)
        self.assertEqual(popen.call_args_list[1].args[0], GRID_BOT_1_COMMAND)
        self.assertEqual(popen.call_args_list[2].args[0], GRID_BOT_2_COMMAND)
        api_process.terminate.assert_called_once()
        bot1_process.terminate.assert_called_once()
        bot2_process.terminate.assert_called_once()

    def test_restart_exhaustion_isolated_to_failed_child(self) -> None:
        failed_process = Mock(spec=subprocess.Popen)
        failed_process.poll.return_value = 1
        failed_process.pid = 200
        failed = Child(
            "grid_bot_1",
            GRID_BOT_1_COMMAND,
            failed_process,
            restart_count=MAX_CHILD_RESTARTS,
        )
        healthy_process = Mock(spec=subprocess.Popen)
        healthy_process.poll.return_value = None
        healthy = Child("grid_bot_2", GRID_BOT_2_COMMAND, healthy_process)

        _supervise_child(failed, {}, now=10)
        _supervise_child(healthy, {}, now=10)

        self.assertTrue(failed.exhausted)
        self.assertFalse(healthy.exhausted)
        healthy_process.terminate.assert_not_called()

    def test_failed_child_restarts_after_bounded_backoff(self) -> None:
        failed_process = Mock(spec=subprocess.Popen)
        failed_process.poll.return_value = 1
        failed_process.pid = 200
        child = Child("grid_bot_1", GRID_BOT_1_COMMAND, failed_process)
        replacement = Mock(spec=subprocess.Popen)
        replacement.pid = 201
        replacement.poll.return_value = None

        _supervise_child(child, {}, now=10)
        self.assertEqual(child.next_restart_at, 11)
        with patch("bot.production_service.subprocess.Popen", return_value=replacement):
            _supervise_child(child, {}, now=10.5)
            self.assertIs(child.process, failed_process)
            _supervise_child(child, {}, now=11)
        self.assertIs(child.process, replacement)
        self.assertEqual(child.restart_count, 1)

    def test_deployment_wiring_uses_reserved_vm_and_production_supervisor(self) -> None:
        repl = tomllib.loads((PROJECT_ROOT / ".replit").read_text(encoding="utf-8"))
        artifact = tomllib.loads(
            (
                PROJECT_ROOT
                / "artifacts"
                / "api-server"
                / ".replit-artifact"
                / "artifact.toml"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(repl["deployment"]["deploymentTarget"], "vm")
        self.assertEqual(
            artifact["services"][0]["production"]["run"]["args"],
            [".pythonlibs/bin/python", "-u", "-m", "bot.production_service"],
        )


if __name__ == "__main__":
    unittest.main()