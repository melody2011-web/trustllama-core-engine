"""Tests for strict environment-backed runtime configuration."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

from solana_webhook_core_rail_pro import cli
from solana_webhook_core_rail_pro.config import Settings, StagingDistribution
from solana_webhook_core_rail_pro.engine import CoreRailEngine
from solana_webhook_core_rail_pro.retry import MAX_RETRY_DELAY_SECONDS
from solana_webhook_core_rail_pro.state import (
    MAX_REPLAY_AUDIT_ENTRIES,
    MAX_RECOVERY_BOUNDARIES,
)


def _environment() -> dict[str, str]:
    return {
        "WEBHOOK_URL": "https://webhook.example.test/events",
        "WEBHOOK_SECRET": "secret-that-is-long-enough",
        "SOLANA_WATCH_ADDRESSES": "11111111111111111111111111111111",
    }


class StagingDistributionTests(unittest.TestCase):
    def test_loads_public_staging_distribution_and_metadata_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            distribution_path = os.path.join(directory, "distribution.toml")
            metadata_path = os.path.join(directory, "metadata.json")
            with open(metadata_path, "w", encoding="utf-8") as metadata:
                metadata.write(
                    '{"entries": [{"name": "product_id", "type": "string"}]}'
                )
            with open(distribution_path, "w", encoding="utf-8") as distribution:
                distribution.write(
                    '[webhook_distribution]\n'
                    'environment = "staging"\n'
                    'visibility = "public"\n'
                    'enabled = true\n'
                    'url = "https://staging.example.test/settlement"\n'
                    'route = "primary-product-settlement"\n'
                    'metadata_layout = "metadata.json"\n'
                )

            result = StagingDistribution.from_file(distribution_path)
            self.assertEqual(result.environment, "staging")
            self.assertEqual(result.visibility, "public")
            self.assertEqual(result.url, "https://staging.example.test/settlement")
            self.assertEqual(result.route, "primary-product-settlement")
            self.assertEqual(result.metadata_layout, "metadata.json")
            self.assertTrue(result.metadata_layout_path.is_file())

    def test_rejects_metadata_layout_that_is_not_an_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            distribution_path = os.path.join(directory, "distribution.toml")
            metadata_path = os.path.join(directory, "metadata.json")
            with open(metadata_path, "w", encoding="utf-8") as metadata:
                metadata.write("[]")
            with open(distribution_path, "w", encoding="utf-8") as distribution:
                distribution.write(
                    '[webhook_distribution]\n'
                    'environment = "staging"\n'
                    'visibility = "public"\n'
                    'enabled = true\n'
                    'url = "https://staging.example.test/settlement"\n'
                    'route = "primary-product-settlement"\n'
                    'metadata_layout = "metadata.json"\n'
                )

            with self.assertRaisesRegex(
                ValueError,
                r"STAGING_DISTRIBUTION\.metadata_layout must contain a JSON object",
            ):
                StagingDistribution.from_file(distribution_path)

    def test_rejects_metadata_layout_with_missing_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            distribution_path = os.path.join(directory, "distribution.toml")
            metadata_path = os.path.join(directory, "metadata.json")
            with open(metadata_path, "w", encoding="utf-8") as metadata:
                metadata.write("{}")
            with open(distribution_path, "w", encoding="utf-8") as distribution:
                distribution.write(
                    '[webhook_distribution]\n'
                    'environment = "staging"\n'
                    'visibility = "public"\n'
                    'enabled = true\n'
                    'url = "https://staging.example.test/settlement"\n'
                    'route = "primary-product-settlement"\n'
                    'metadata_layout = "metadata.json"\n'
                )

            with self.assertRaisesRegex(
                ValueError,
                r"STAGING_DISTRIBUTION\.metadata_layout\.entries is required",
            ):
                StagingDistribution.from_file(distribution_path)

    def test_rejects_metadata_entry_schema_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            distribution_path = os.path.join(directory, "distribution.toml")
            metadata_path = os.path.join(directory, "metadata.json")
            with open(metadata_path, "w", encoding="utf-8") as metadata:
                metadata.write('{"entries": [{"name": "product_id"}]}')
            with open(distribution_path, "w", encoding="utf-8") as distribution:
                distribution.write(
                    '[webhook_distribution]\n'
                    'environment = "staging"\n'
                    'visibility = "public"\n'
                    'enabled = true\n'
                    'url = "https://staging.example.test/settlement"\n'
                    'route = "primary-product-settlement"\n'
                    'metadata_layout = "metadata.json"\n'
                )

            with self.assertRaisesRegex(
                ValueError,
                r"STAGING_DISTRIBUTION\.metadata_layout\.entries\[0\]\.type",
            ):
                StagingDistribution.from_file(distribution_path)

    def test_rejects_unsupported_metadata_entry_type(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            distribution_path = os.path.join(directory, "distribution.toml")
            metadata_path = os.path.join(directory, "metadata.json")
            with open(metadata_path, "w", encoding="utf-8") as metadata:
                metadata.write(
                    '{"entries": [{"name": "product_id", "type": "uuid"}]}'
                )
            with open(distribution_path, "w", encoding="utf-8") as distribution:
                distribution.write(
                    '[webhook_distribution]\n'
                    'environment = "staging"\n'
                    'visibility = "public"\n'
                    'enabled = true\n'
                    'url = "https://staging.example.test/settlement"\n'
                    'route = "primary-product-settlement"\n'
                    'metadata_layout = "metadata.json"\n'
                )

            with self.assertRaisesRegex(
                ValueError,
                r"STAGING_DISTRIBUTION\.metadata_layout\.entries\[0\]\.type "
                r"has unsupported value",
            ):
                StagingDistribution.from_file(distribution_path)

    def test_rejects_non_https_public_distribution_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            distribution_path = os.path.join(directory, "distribution.toml")
            metadata_path = os.path.join(directory, "metadata.json")
            with open(metadata_path, "w", encoding="utf-8") as metadata:
                metadata.write("{}")
            with open(distribution_path, "w", encoding="utf-8") as distribution:
                distribution.write(
                    '[webhook_distribution]\n'
                    'environment = "staging"\n'
                    'visibility = "public"\n'
                    'enabled = true\n'
                    'url = "http://staging.example.test"\n'
                    'route = "settlement"\n'
                    'metadata_layout = "metadata.json"\n'
                )

            with self.assertRaisesRegex(
                ValueError,
                r"STAGING_DISTRIBUTION\.url must be an absolute HTTPS URL",
            ):
                StagingDistribution.from_file(distribution_path)

    def test_rejects_empty_route_and_missing_metadata_layout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            distribution_path = os.path.join(directory, "distribution.toml")
            with open(distribution_path, "w", encoding="utf-8") as distribution:
                distribution.write(
                    '[webhook_distribution]\n'
                    'environment = "staging"\n'
                    'visibility = "public"\n'
                    'enabled = true\n'
                    'url = "https://staging.example.test"\n'
                    'route = ""\n'
                    'metadata_layout = "missing.json"\n'
                )

            with self.assertRaisesRegex(
                ValueError,
                r"STAGING_DISTRIBUTION\.route must be a non-empty string",
            ):
                StagingDistribution.from_file(distribution_path)

            with open(distribution_path, "w", encoding="utf-8") as distribution:
                distribution.write(
                    '[webhook_distribution]\n'
                    'environment = "staging"\n'
                    'visibility = "public"\n'
                    'enabled = true\n'
                    'url = "https://staging.example.test"\n'
                    'route = "settlement"\n'
                    'metadata_layout = "missing.json"\n'
                )

            with self.assertRaisesRegex(
                ValueError,
                r"STAGING_DISTRIBUTION\.metadata_layout file does not exist",
            ):
                StagingDistribution.from_file(distribution_path)


class SettingsTests(unittest.TestCase):
    def test_positive_float_settings_load_finite_values(self) -> None:
        environment = {
            **_environment(),
            "WEBHOOK_TIMEOUT_SECONDS": "12.5",
            "WEBHOOK_BASE_BACKOFF_SECONDS": "3.25",
            "SOLANA_POLL_INTERVAL_SECONDS": "4.75",
            "BLOCK_PROCESSING_LATENCY_WARNING_THRESHOLD_MS": "750",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.webhook_timeout_seconds, 12.5)
        self.assertEqual(settings.webhook_base_backoff_seconds, 3.25)
        self.assertEqual(settings.poll_interval_seconds, 4.75)
        self.assertEqual(
            settings.block_processing_latency_warning_threshold_ms,
            750.0,
        )
        self.assertEqual(settings.settlement_route, "primary-product-settlement")

    def test_direct_settings_reject_non_finite_runtime_limits(self) -> None:
        settings = {
            "rpc_urls": ("https://rpc.example.test",),
            "commitment": "confirmed",
            "watch_addresses": (),
            "watch_signatures": (),
            "webhook_url": "https://webhook.example.test/events",
            "webhook_secret": "secret-that-is-long-enough",
        }
        runtime_limits = (
            "webhook_timeout_seconds",
            "webhook_base_backoff_seconds",
            "poll_interval_seconds",
            "block_processing_latency_warning_threshold_ms",
        )

        for field in runtime_limits:
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(
                        ValueError,
                        rf"{field} must be a finite number",
                    ):
                        Settings(**settings, **{field: value})

    def test_direct_settings_accept_positive_runtime_limits(self) -> None:
        settings = Settings(
            rpc_urls=("https://rpc.example.test",),
            commitment="confirmed",
            watch_addresses=(),
            watch_signatures=(),
            webhook_url="https://webhook.example.test/events",
            webhook_secret="secret-that-is-long-enough",
            webhook_timeout_seconds=12.5,
            webhook_base_backoff_seconds=3.25,
            poll_interval_seconds=4.75,
            block_processing_latency_warning_threshold_ms=750.0,
        )

        self.assertEqual(settings.webhook_timeout_seconds, 12.5)
        self.assertEqual(settings.webhook_base_backoff_seconds, 3.25)
        self.assertEqual(settings.poll_interval_seconds, 4.75)
        self.assertEqual(
            settings.block_processing_latency_warning_threshold_ms,
            750.0,
        )

    def test_settings_reject_retry_delay_above_practical_limit(self) -> None:
        settings = {
            "rpc_urls": ("https://rpc.example.test",),
            "commitment": "confirmed",
            "watch_addresses": (),
            "watch_signatures": (),
            "webhook_url": "https://webhook.example.test/events",
            "webhook_secret": "secret-that-is-long-enough",
            "webhook_max_attempts": 2,
        }

        with self.assertRaisesRegex(
            ValueError,
            rf"retry backoff delay must not exceed {MAX_RETRY_DELAY_SECONDS} seconds",
        ):
            Settings(
                **settings,
                webhook_base_backoff_seconds=MAX_RETRY_DELAY_SECONDS + 1,
            )

    def test_from_env_rejects_retry_delay_above_practical_limit(self) -> None:
        environment = {
            **_environment(),
            "WEBHOOK_MAX_ATTEMPTS": "2",
            "WEBHOOK_BASE_BACKOFF_SECONDS": str(MAX_RETRY_DELAY_SECONDS + 1),
        }

        with (
            patch.dict(os.environ, environment, clear=True),
            self.assertRaisesRegex(
                ValueError,
                rf"retry backoff delay must not exceed {MAX_RETRY_DELAY_SECONDS} seconds",
            ),
        ):
            Settings.from_env()

    def test_settings_accept_retry_delay_at_practical_limit(self) -> None:
        settings = Settings(
            rpc_urls=("https://rpc.example.test",),
            commitment="confirmed",
            watch_addresses=(),
            watch_signatures=(),
            webhook_url="https://webhook.example.test/events",
            webhook_secret="secret-that-is-long-enough",
            webhook_max_attempts=2,
            webhook_base_backoff_seconds=MAX_RETRY_DELAY_SECONDS,
        )

        self.assertEqual(
            settings.webhook_base_backoff_seconds,
            MAX_RETRY_DELAY_SECONDS,
        )

    def test_recovery_boundary_capacity_defaults_to_state_limit(self) -> None:
        with patch.dict(os.environ, _environment(), clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.max_recovery_boundaries, MAX_RECOVERY_BOUNDARIES)

    def test_replay_audit_capacity_defaults_to_state_limit(self) -> None:
        with patch.dict(os.environ, _environment(), clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.replay_audit_max_entries, MAX_REPLAY_AUDIT_ENTRIES)

    def test_replay_audit_capacity_is_loaded_and_validated(self) -> None:
        environment = {**_environment(), "REPLAY_AUDIT_MAX_ENTRIES": "7"}
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.replay_audit_max_entries, 7)

        for value, message in (("0", "greater than zero"), ("not-an-integer", "an integer")):
            with self.subTest(value=value):
                invalid_environment = {
                    **_environment(),
                    "REPLAY_AUDIT_MAX_ENTRIES": value,
                }
                with patch.dict(os.environ, invalid_environment, clear=True):
                    with self.assertRaisesRegex(
                        ValueError,
                        rf"REPLAY_AUDIT_MAX_ENTRIES must be {message}",
                    ):
                        Settings.from_env()

    def test_recovery_boundary_capacity_is_loaded_from_environment(self) -> None:
        environment = {**_environment(), "RECOVERY_BOUNDARY_MAX_ENTRIES": "7"}
        with patch.dict(os.environ, environment, clear=True):
            settings = Settings.from_env()

        self.assertEqual(settings.max_recovery_boundaries, 7)

    def test_recovery_boundary_capacity_rejects_invalid_values(self) -> None:
        for value, message in (("0", "greater than zero"), ("not-an-integer", "an integer")):
            with self.subTest(value=value):
                environment = {
                    **_environment(),
                    "RECOVERY_BOUNDARY_MAX_ENTRIES": value,
                }
                with patch.dict(os.environ, environment, clear=True):
                    with self.assertRaisesRegex(
                        ValueError,
                        rf"RECOVERY_BOUNDARY_MAX_ENTRIES must be {message}",
                    ):
                        Settings.from_env()

    def test_engine_exposes_configured_capacity_in_dashboard_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                rpc_urls=("https://rpc.example.test",),
                commitment="confirmed",
                watch_addresses=(),
                watch_signatures=(),
                webhook_url="https://webhook.example.test/events",
                webhook_secret="secret-that-is-long-enough",
                max_recovery_boundaries=7,
                state_path=f"{directory}/state.sqlite3",
            )
            engine = CoreRailEngine(
                settings,
                rpc=object(),  # type: ignore[arg-type]
                delivery=object(),  # type: ignore[arg-type]
            )
            try:
                self.assertEqual(
                    engine.metrics.snapshot()["recovery"]["active_evidence_limit"],
                    7,
                )
            finally:
                engine._state.close()  # type: ignore[attr-defined]


class CliStartupTests(unittest.TestCase):
    def test_invalid_staging_distribution_exits_before_network_clients_are_created(
        self,
    ) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    **_environment(),
                    "STAGING_DISTRIBUTION_PATH": "/does/not/exist/distribution.toml",
                },
                clear=True,
            ),
            patch("sys.argv", ["solana-webhook-core-rail-pro"]),
            patch.object(cli, "SolanaRpcClient") as rpc_client,
            patch.object(cli, "WebhookDelivery") as webhook_delivery,
        ):
            with self.assertRaisesRegex(
                SystemExit,
                r"Configuration error: STAGING_DISTRIBUTION_PATH file does not exist",
            ):
                cli.main()

        rpc_client.assert_not_called()
        webhook_delivery.assert_not_called()

    def test_invalid_runtime_settings_exit_before_network_clients_are_created(
        self,
    ) -> None:
        invalid_settings = (
            ("RECOVERY_BOUNDARY_MAX_ENTRIES", "0", "must be greater than zero"),
            ("WEBHOOK_TIMEOUT_SECONDS", "0", "must be greater than zero"),
            ("WEBHOOK_TIMEOUT_SECONDS", "nan", "must be a finite number"),
            ("WEBHOOK_TIMEOUT_SECONDS", "inf", "must be a finite number"),
            ("WEBHOOK_TIMEOUT_SECONDS", "-inf", "must be a finite number"),
            ("WEBHOOK_BASE_BACKOFF_SECONDS", "nan", "must be a finite number"),
            ("WEBHOOK_BASE_BACKOFF_SECONDS", "inf", "must be a finite number"),
            ("WEBHOOK_BASE_BACKOFF_SECONDS", "-inf", "must be a finite number"),
            ("SOLANA_POLL_INTERVAL_SECONDS", "nan", "must be a finite number"),
            ("SOLANA_POLL_INTERVAL_SECONDS", "inf", "must be a finite number"),
            ("SOLANA_POLL_INTERVAL_SECONDS", "-inf", "must be a finite number"),
            (
                "BLOCK_PROCESSING_LATENCY_WARNING_THRESHOLD_MS",
                "0",
                "must be greater than zero",
            ),
            (
                "BLOCK_PROCESSING_LATENCY_WARNING_THRESHOLD_MS",
                "nan",
                "must be a finite number",
            ),
            (
                "BLOCK_PROCESSING_LATENCY_WARNING_THRESHOLD_MS",
                "inf",
                "must be a finite number",
            ),
            (
                "BLOCK_PROCESSING_LATENCY_WARNING_THRESHOLD_MS",
                "-inf",
                "must be a finite number",
            ),
            ("SOLANA_MAX_RPC_CONCURRENCY", "0", "must be greater than zero"),
            ("WEBHOOK_MAX_CONCURRENCY", "0", "must be greater than zero"),
            ("EVENT_QUEUE_SIZE", "0", "must be greater than zero"),
            ("SOLANA_RPC_URL", "not-a-url", "must be an absolute http\\(s\\) URL"),
            (
                "WEBHOOK_URL",
                "ftp://webhook.example.test/events",
                "must be an absolute http\\(s\\) URL",
            ),
            (
                "SOLANA_COMMITMENT",
                "rooted",
                "must be processed, confirmed, or finalized",
            ),
        )

        for setting, value, constraint in invalid_settings:
            with self.subTest(setting=setting, value=value):
                environment = {
                    **_environment(),
                    setting: value,
                }
                with (
                    patch.dict(os.environ, environment, clear=True),
                    patch("sys.argv", ["solana-webhook-core-rail-pro"]),
                    patch.object(cli, "SolanaRpcClient") as rpc_client,
                    patch.object(cli, "WebhookDelivery") as webhook_delivery,
                ):
                    with self.assertRaisesRegex(
                        SystemExit,
                        rf"Configuration error: {setting} {constraint}",
                    ):
                        cli.main()

                rpc_client.assert_not_called()
                webhook_delivery.assert_not_called()


if __name__ == "__main__":
    unittest.main()