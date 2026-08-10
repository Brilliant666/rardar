from __future__ import annotations

import io
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from pipeline.deployment import (
    CANONICAL_SYSTEMD_PATHS,
    DeploymentCheckError,
    _check_release,
    _check_toolchain,
    _check_vinext_state,
    _load_paths,
    check_offline,
    check_online,
    main,
)
from pipeline.data_lock import manager_dir_lock_path
from pipeline.generations import resolve_current_generation
from pipeline.runtime import acquire_manager_lock, release_manager_lock
from pipeline.stable_read import StableReadError


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_UNIT = REPOSITORY_ROOT / "deploy" / "systemd" / "rardar.service"
SYSTEMD_ENV = REPOSITORY_ROOT / "deploy" / "systemd" / "rardar.env.example"


def _write_release_fixture(home: Path) -> None:
    for relative in (
        "package.json",
        "package-lock.json",
        "pipeline/runtime.py",
        "node_modules/vinext/dist/cli.js",
        "node_modules/vite/bin/vite.js",
        "vite.config.ts",
        ".openai/hosting.json",
        "app/runtime-readiness.mjs",
        "build/published-data-bridge.ts",
        "build/sites-vite-plugin.ts",
        "worker/index.ts",
    ):
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture\n", encoding="utf-8")
    (home / "dist").mkdir(parents=True)
    (home / "deploy" / "systemd").mkdir(parents=True)


def _copy_current_generation(target: Path) -> str:
    source = resolve_current_generation(REPOSITORY_ROOT / "data")
    if source.legacy or source.generation_id is None:
        raise AssertionError("deployment tests require the checked-in generation fixture")
    destination = target / "generations" / source.generation_id
    destination.parent.mkdir(parents=True)
    shutil.copytree(source.root, destination)
    shutil.copy2(source.data_dir / "current.json", target / "current.json")
    resolved = resolve_current_generation(target)
    if resolved.generation_id != source.generation_id:
        raise AssertionError("isolated generation fixture did not resolve")
    return source.generation_id


def _create_rardar_database(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(
            """
            CREATE TABLE feedback (id INTEGER PRIMARY KEY);
            CREATE TABLE decision_events (id INTEGER PRIMARY KEY);
            CREATE TABLE project_actions (id INTEGER PRIMARY KEY);
            CREATE TABLE project_action_events_v2 (id INTEGER PRIMARY KEY);
            """
        )
        connection.commit()
    finally:
        connection.close()


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def _tree_state(root: Path) -> dict[str, tuple[int, int, int, bytes | None]]:
    """Capture entries, bytes, modes and mtimes without making atime significant."""

    state: dict[str, tuple[int, int, int, bytes | None]] = {}
    for path in (root, *sorted(root.rglob("*"))):
        metadata = path.lstat()
        relative = "." if path == root else path.relative_to(root).as_posix()
        content = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        state[relative] = (
            metadata.st_mode,
            metadata.st_size,
            metadata.st_mtime_ns,
            content,
        )
    return state


class DeploymentFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.paths = {
            "home": root / "home",
            "data": root / "data",
            "runtime": root / "runtime",
            "state": root / "vinext-state",
            "locks": root / "locks",
            "cache": root / "vite-cache",
            "backups": root / "backups",
            "wrangler_logs": root / "wrangler-logs",
            "wrangler_registry": root / "wrangler-registry",
            "miniflare_registry": root / "miniflare-registry",
            "scratch": root / "sqlite-scratch",
        }
        for path in self.paths.values():
            path.mkdir()
        _write_release_fixture(self.paths["home"])
        application_root = patch("pipeline.deployment.APPLICATION_ROOT", self.paths["home"])
        application_root.start()
        self.addCleanup(application_root.stop)
        self.generation_id: str | None = None
        _create_rardar_database(self.paths["state"] / "d1" / "rardar.sqlite")
        self.environment = {
            "RARDAR_HOME": str(self.paths["home"]),
            "RARDAR_DATA_DIR": str(self.paths["data"]),
            "RARDAR_RUNTIME_DIR": str(self.paths["runtime"]),
            "RARDAR_VINEXT_STATE_DIR": str(self.paths["state"]),
            "RARDAR_DATA_LOCK_DIR": str(self.paths["locks"]),
            "RARDAR_VITE_CACHE_DIR": str(self.paths["cache"]),
            "RARDAR_BACKUP_DIR": str(self.paths["backups"]),
            "RARDAR_PYTHON": str(Path(sys.executable).resolve()),
            "RARDAR_NODE": str(Path(sys.executable).resolve()),
            "RARDAR_DEPLOY_MIN_FREE_BYTES": "1",
            "RARDAR_VINEXT_PORT": "39101",
            "RARDAR_RUNTIME_STATUS_PORT": "39102",
            "RARDAR_SCHEDULE_AT": "06:45",
            "RARDAR_SCHEDULE_TIMEZONE": "Europe/Berlin",
            "RARDAR_STALE_AFTER_HOURS": "48",
            "WRANGLER_LOG_PATH": str(self.paths["wrangler_logs"]),
            "WRANGLER_REGISTRY_PATH": str(self.paths["wrangler_registry"]),
            "MINIFLARE_REGISTRY_PATH": str(self.paths["miniflare_registry"]),
        }
        canonical_layout = patch.dict(
            CANONICAL_SYSTEMD_PATHS,
            {
                "RARDAR_HOME": self.paths["home"],
                "RARDAR_DATA_DIR": self.paths["data"],
                "RARDAR_RUNTIME_DIR": self.paths["runtime"],
                "RARDAR_VINEXT_STATE_DIR": self.paths["state"],
                "RARDAR_DATA_LOCK_DIR": self.paths["locks"],
                "RARDAR_VITE_CACHE_DIR": self.paths["cache"],
                "RARDAR_BACKUP_DIR": self.paths["backups"],
                "WRANGLER_LOG_PATH": self.paths["wrangler_logs"],
                "WRANGLER_REGISTRY_PATH": self.paths["wrangler_registry"],
                "MINIFLARE_REGISTRY_PATH": self.paths["miniflare_registry"],
            },
            clear=True,
        )
        canonical_layout.start()
        self.addCleanup(canonical_layout.stop)
        sqlite_scratch_root = patch(
            "pipeline.deployment.SYSTEMD_SQLITE_SCRATCH_ROOT",
            self.paths["scratch"],
        )
        sqlite_scratch_root.start()
        self.addCleanup(sqlite_scratch_root.stop)

    def ensure_generation(self) -> str:
        if self.generation_id is None:
            self.generation_id = _copy_current_generation(self.paths["data"])
        return self.generation_id

    def healthy_status(self, *, stale: bool = False) -> dict[str, object]:
        generation_id = self.ensure_generation()
        now = datetime.now(timezone.utc)
        return {
            "schemaVersion": 1,
            "state": "degraded" if stale else "healthy",
            "checkedAt": now.isoformat(),
            "managerPid": 101,
            "data": {
                "freshness": "stale" if stale else "fresh",
                "currentGenerationId": generation_id,
                "staleAfterSeconds": 48 * 60 * 60,
            },
            "services": {
                "website": {
                    "state": "healthy",
                    "pid": 102,
                    "generationId": generation_id,
                },
                "scheduler": {
                    "state": "healthy",
                    "pid": 103,
                    "currentGenerationId": generation_id,
                    "telemetryTrusted": True,
                    "reportedProcessId": 103,
                    "heartbeatAt": now.isoformat(),
                },
            },
            "schedule": {
                "at": "06:45",
                "timezone": "Europe/Berlin",
                "nextRunAt": (now + timedelta(days=1)).isoformat(),
            },
            "runtime": {
                "host": "127.0.0.1",
                "home": str(self.paths["home"].resolve()),
                "dataDir": str(self.paths["data"].resolve()),
                "runtimeDir": str(self.paths["runtime"].resolve()),
                "dataLockDir": str(self.paths["locks"].resolve()),
                "vinextPort": 39101,
                "runtimeStatusPort": 39102,
                "statusUrl": "http://127.0.0.1:39102/status",
            },
        }

    def healthy_health(self, *, stale: bool = False) -> dict[str, object]:
        return {
            "status": "degraded" if stale else "healthy",
            **({"reason": "published_data_stale"} if stale else {}),
            "generationId": self.ensure_generation(),
            "data": {
                "freshness": "stale" if stale else "fresh",
                "staleAfterSeconds": 48 * 60 * 60,
            },
            "schedule": {"at": "06:45", "timezone": "Europe/Berlin"},
        }

    @staticmethod
    def toolchain_payload() -> dict[str, str]:
        return {
            "python": "3.12.0",
            "pythonPath": sys.executable,
            "node": "22.13.0",
            "nodePath": sys.executable,
        }


class OfflineDeploymentTests(DeploymentFixture):
    def test_offline_check_is_complete_and_does_not_change_persistent_bytes(self) -> None:
        generation_id = self.ensure_generation()
        unrelated = self.paths["state"] / "metadata.sqlite"
        connection = sqlite3.connect(unrelated)
        try:
            connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
            connection.commit()
        finally:
            connection.close()
        before = {
            name: _tree_bytes(path)
            for name, path in self.paths.items()
            if name != "home"
        }

        with patch(
            "pipeline.deployment._check_toolchain",
            return_value=self.toolchain_payload(),
        ):
            report = check_offline(self.environment)

        after = {
            name: _tree_bytes(path)
            for name, path in self.paths.items()
            if name != "home"
        }
        self.assertEqual(before, after)
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["generation"]["generationId"], generation_id)
        self.assertEqual(report["generation"]["schema"], "healthy")
        self.assertEqual(report["generation"]["auditErrorCount"], 0)
        self.assertEqual(report["d1"]["sqliteFileCount"], 2)
        self.assertEqual(report["d1"]["rardarDatabaseCount"], 1)
        self.assertEqual(report["locks"]["status"], "available")
        self.assertEqual(_tree_bytes(self.paths["runtime"]), {})
        self.assertEqual(_tree_bytes(self.paths["locks"]), {})

    def test_offline_requires_manager_scheduler_and_writer_locks_to_be_idle(self) -> None:
        with patch(
            "pipeline.deployment._check_toolchain",
            return_value=self.toolchain_payload(),
        ), patch(
            "pipeline.deployment._probe_lock_file",
            side_effect=DeploymentCheckError("deployment_lock_held", "fixture lock"),
        ):
            with self.assertRaises(DeploymentCheckError) as raised:
                check_offline(self.environment)
        self.assertEqual(raised.exception.code, "deployment_lock_held")

    def test_offline_fails_when_the_real_canonical_manager_lock_is_held(self) -> None:
        lock_path = manager_dir_lock_path(self.paths["data"], self.paths["locks"])
        handle = acquire_manager_lock(lock_path)
        self.assertIsNotNone(handle)
        try:
            with patch(
                "pipeline.deployment._check_toolchain",
                return_value=self.toolchain_payload(),
            ):
                with self.assertRaises(DeploymentCheckError) as raised:
                    check_offline(self.environment)
            self.assertEqual(raised.exception.code, "deployment_lock_held")
            self.assertIn(str(lock_path), raised.exception.detail)
        finally:
            if handle is not None:
                release_manager_lock(handle)

    def test_runtime_contract_requires_explicit_canonical_settings_and_paths(self) -> None:
        required = (
            "RARDAR_VINEXT_PORT",
            "RARDAR_RUNTIME_STATUS_PORT",
            "RARDAR_SCHEDULE_AT",
            "RARDAR_SCHEDULE_TIMEZONE",
            "RARDAR_STALE_AFTER_HOURS",
            "RARDAR_VITE_CACHE_DIR",
            "WRANGLER_LOG_PATH",
            "WRANGLER_REGISTRY_PATH",
            "MINIFLARE_REGISTRY_PATH",
        )
        for name in required:
            with self.subTest(missing=name):
                environment = dict(self.environment)
                environment.pop(name)
                with self.assertRaises(DeploymentCheckError) as raised:
                    check_offline(environment)
                self.assertEqual(raised.exception.code, "runtime_configuration_missing")

        invalid = {
            "RARDAR_VINEXT_PORT": "039101",
            "RARDAR_RUNTIME_STATUS_PORT": "0",
            "RARDAR_SCHEDULE_AT": "6:45",
            "RARDAR_SCHEDULE_TIMEZONE": "Not/AZone",
            "RARDAR_STALE_AFTER_HOURS": "048",
            "WRANGLER_LOG_PATH": "relative/wrangler",
        }
        for name, value in invalid.items():
            with self.subTest(invalid=name):
                environment = {**self.environment, name: value}
                with self.assertRaises(DeploymentCheckError) as raised:
                    check_offline(environment)
                self.assertIn(
                    raised.exception.code,
                    {"runtime_configuration_invalid", "deployment_path_not_absolute"},
                )

    def test_all_deployment_paths_must_be_absolute_and_non_overlapping(self) -> None:
        relative = dict(self.environment)
        relative["RARDAR_RUNTIME_DIR"] = "runtime"
        with self.assertRaises(DeploymentCheckError) as raised:
            _load_paths(relative)
        self.assertEqual(raised.exception.code, "deployment_path_not_absolute")

        nested = self.paths["data"] / "runtime"
        nested.mkdir()
        overlapping = dict(self.environment)
        overlapping["RARDAR_RUNTIME_DIR"] = str(nested)
        with self.assertRaises(DeploymentCheckError) as raised:
            _load_paths(overlapping)
        self.assertEqual(raised.exception.code, "deployment_paths_overlap")

    def test_deployment_path_with_symlink_component_is_rejected(self) -> None:
        outside = Path(self.temporary.name) / "outside-state"
        outside.mkdir()
        linked = Path(self.temporary.name) / "linked-state"
        try:
            linked.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"directory symlinks are unavailable: {error}")
        environment = dict(self.environment)
        environment["RARDAR_VINEXT_STATE_DIR"] = str(linked)
        with self.assertRaises(DeploymentCheckError) as raised:
            _load_paths(environment)
        self.assertEqual(raised.exception.code, "deployment_path_symlink")

    def test_home_allows_only_a_leaf_release_symlink_and_records_both_paths(self) -> None:
        linked = Path(self.temporary.name) / "current"
        try:
            linked.symlink_to(self.paths["home"], target_is_directory=True)
        except OSError as error:
            self.skipTest(f"directory symlinks are unavailable: {error}")
        environment = dict(self.environment)
        environment["RARDAR_HOME"] = str(linked)
        paths = _load_paths(environment)
        self.assertEqual(paths.home_configured, linked.absolute())
        self.assertEqual(paths.home, self.paths["home"].resolve())
        self.assertEqual(paths.as_dict()["homeConfigured"], str(linked.absolute()))
        self.assertEqual(paths.as_dict()["homeResolved"], str(self.paths["home"].resolve()))

    def test_home_leaf_symlink_rejects_a_second_symlink_in_its_target_chain(self) -> None:
        alias = Path(self.temporary.name) / "release-alias"
        current = Path(self.temporary.name) / "current-chain"
        try:
            alias.symlink_to(self.paths["home"], target_is_directory=True)
            current.symlink_to(alias, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"directory symlinks are unavailable: {error}")
        environment = {**self.environment, "RARDAR_HOME": str(current)}
        with self.assertRaises(DeploymentCheckError) as raised:
            _load_paths(environment)
        self.assertEqual(raised.exception.code, "deployment_path_symlink")

    def test_release_required_path_rejects_a_symlinked_ancestor(self) -> None:
        external = Path(self.temporary.name) / "external-node-modules"
        external.mkdir()
        shutil.rmtree(self.paths["home"] / "node_modules")
        try:
            (self.paths["home"] / "node_modules").symlink_to(
                external,
                target_is_directory=True,
            )
        except OSError as error:
            self.skipTest(f"directory symlinks are unavailable: {error}")
        with self.assertRaises(DeploymentCheckError) as raised:
            _check_release(self.paths["home"])
        self.assertEqual(raised.exception.code, "release_path_symlink")

    def test_release_symlink_ancestor_precedes_missing_descendants_portably(self) -> None:
        node_modules = (self.paths["home"] / "node_modules").resolve(strict=True)
        shutil.rmtree(node_modules)
        original_lstat = os.lstat
        symlink_metadata = os.stat_result(
            (stat.S_IFLNK | 0o777, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        )

        def emulate_symlink(path, *args, **kwargs):
            if Path(path) == node_modules:
                return symlink_metadata
            return original_lstat(path, *args, **kwargs)

        with patch("pipeline.deployment.os.lstat", side_effect=emulate_symlink):
            with self.assertRaises(DeploymentCheckError) as raised:
                _check_release(self.paths["home"])
        self.assertEqual(raised.exception.code, "release_path_symlink")

    def test_release_missing_descendant_without_symlink_stays_incomplete(self) -> None:
        (self.paths["home"] / "node_modules" / "vite" / "bin" / "vite.js").unlink()
        with self.assertRaises(DeploymentCheckError) as raised:
            _check_release(self.paths["home"])
        self.assertEqual(raised.exception.code, "release_incomplete")

    def test_release_rejects_a_required_file_that_cannot_be_read_stably(self) -> None:
        def fail_target(path: Path, **_kwargs):
            if path.name == "runtime.py" and path.parent.name == "pipeline":
                raise StableReadError(
                    "concurrent_change",
                    path,
                    "deterministic in-place mutation",
                    retryable=True,
                )
            from pipeline.stable_read import stable_read as real_stable_read

            return real_stable_read(path)

        with patch("pipeline.deployment.stable_read", side_effect=fail_target):
            with self.assertRaises(DeploymentCheckError) as raised:
                _check_release(self.paths["home"])

        self.assertEqual(raised.exception.code, "release_file_unstable")

    def test_release_requires_vite_config_and_real_environment_directory(self) -> None:
        for relative in ("vite.config.ts", "deploy/systemd"):
            target = self.paths["home"] / relative
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            with self.subTest(relative=relative):
                with self.assertRaises(DeploymentCheckError) as raised:
                    _check_release(self.paths["home"])
                self.assertEqual(raised.exception.code, "release_incomplete")
            if relative == "vite.config.ts":
                target.write_text("fixture\n", encoding="utf-8")
            else:
                target.mkdir(parents=True)

    def test_release_rejects_symlinked_vite_config_and_environment_directory(self) -> None:
        external_file = Path(self.temporary.name) / "external-vite.config.ts"
        external_file.write_text("fixture\n", encoding="utf-8")
        external_directory = Path(self.temporary.name) / "external-env-dir"
        external_directory.mkdir()
        pairs = (
            (self.paths["home"] / "vite.config.ts", external_file, False),
            (self.paths["home"] / "deploy" / "systemd", external_directory, True),
        )
        for target, external, is_directory in pairs:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            try:
                target.symlink_to(external, target_is_directory=is_directory)
            except OSError as error:
                self.skipTest(f"symlinks are unavailable: {error}")
            with self.subTest(relative=str(target.relative_to(self.paths["home"]))):
                with self.assertRaises(DeploymentCheckError) as raised:
                    _check_release(self.paths["home"])
                self.assertEqual(raised.exception.code, "release_path_symlink")
            target.unlink()
            if is_directory:
                target.mkdir(parents=True)
            else:
                target.write_text("fixture\n", encoding="utf-8")

    def test_release_rejects_ignored_environment_and_dev_vars_files(self) -> None:
        allowed_example = self.paths["home"] / ".env.production.example"
        allowed_example.write_text("SAFE_EXAMPLE=\n", encoding="utf-8")
        self.assertGreater(_check_release(self.paths["home"])["requiredPathCount"], 0)

        candidates = (
            self.paths["home"] / ".env",
            self.paths["home"] / ".env.local",
            self.paths["home"] / ".dev.vars",
            self.paths["home"] / ".dev.vars.production",
            self.paths["home"] / "deploy" / "systemd" / ".env.development",
        )
        for candidate in candidates:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text("UNTRUSTED=value\n", encoding="utf-8")
            with self.subTest(path=str(candidate.relative_to(self.paths["home"]))):
                with self.assertRaises(DeploymentCheckError) as raised:
                    _check_release(self.paths["home"])
                self.assertEqual(
                    raised.exception.code,
                    "release_environment_file_forbidden",
                )
            candidate.unlink()

    def test_vinext_state_scans_all_sqlite_files_and_requires_rardar_tables(self) -> None:
        other_root = Path(self.temporary.name) / "other-state"
        other_root.mkdir()
        connection = sqlite3.connect(other_root / "other.sqlite")
        try:
            connection.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(DeploymentCheckError) as raised:
            _check_vinext_state(other_root)
        self.assertEqual(raised.exception.code, "rardar_d1_database_missing")

    def test_vinext_state_requires_exactly_one_rardar_schema_database(self) -> None:
        source = self.paths["state"] / "d1" / "rardar.sqlite"
        duplicate = self.paths["state"] / "duplicate.sqlite"
        shutil.copy2(source, duplicate)
        with self.assertRaises(DeploymentCheckError) as raised:
            _check_vinext_state(self.paths["state"])
        self.assertEqual(raised.exception.code, "rardar_d1_database_ambiguous")

    def test_offline_sqlite_wal_and_shm_check_is_byte_and_metadata_read_only(self) -> None:
        self.ensure_generation()
        database = self.paths["state"] / "d1" / "rardar.sqlite"
        connection = sqlite3.connect(database)
        try:
            journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if not journal_mode or str(journal_mode[0]).lower() != "wal":
                self.skipTest("SQLite WAL mode is unavailable")
            connection.execute("PRAGMA wal_autocheckpoint = 0")
            connection.execute("INSERT INTO feedback DEFAULT VALUES")
            connection.commit()
            sidecars = (
                database.with_name(database.name + "-wal"),
                database.with_name(database.name + "-shm"),
            )
            if not all(path.exists() for path in sidecars):
                self.skipTest("SQLite did not retain WAL and SHM sidecars")
            before = _tree_state(self.paths["state"])
            with patch(
                "pipeline.deployment._check_toolchain",
                return_value=self.toolchain_payload(),
            ):
                report = check_offline(self.environment)
            after = _tree_state(self.paths["state"])
            self.assertEqual(before, after)
            self.assertEqual(report["d1"]["rardarDatabaseCount"], 1)
        finally:
            connection.close()

    def test_offline_reads_a_fresh_schema_that_exists_only_in_wal_without_writing_source(self) -> None:
        wal_root = Path(self.temporary.name) / "wal-only-state"
        wal_root.mkdir()
        database = wal_root / "rardar.sqlite"
        connection = sqlite3.connect(database)
        try:
            journal_mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            if not journal_mode or str(journal_mode[0]).lower() != "wal":
                self.skipTest("SQLite WAL mode is unavailable")
            connection.execute("PRAGMA wal_autocheckpoint = 0")
            connection.executescript(
                """
                CREATE TABLE feedback (id INTEGER PRIMARY KEY);
                CREATE TABLE decision_events (id INTEGER PRIMARY KEY);
                CREATE TABLE project_actions (id INTEGER PRIMARY KEY);
                """
            )
            connection.commit()
            before = _tree_state(wal_root)
            report = _check_vinext_state(wal_root)
            after = _tree_state(wal_root)
            self.assertEqual(before, after)
            self.assertEqual(report["rardarDatabaseCount"], 1)
        finally:
            connection.close()

    def test_sqlite_scratch_ignores_temp_environment_and_preserves_source(self) -> None:
        self.ensure_generation()
        source_before = _tree_state(self.paths["state"])
        created_scratch: list[Path] = []
        original_temporary_directory = tempfile.TemporaryDirectory

        def recording_temporary_directory(*args, **kwargs):
            self.assertEqual(
                Path(kwargs["dir"]).resolve(),
                self.paths["scratch"].resolve(),
            )
            context = original_temporary_directory(*args, **kwargs)
            created_scratch.append(Path(context.name).resolve())
            return context

        poisoned_environment = {
            **self.environment,
            "TMPDIR": str(self.paths["state"]),
            "TEMP": str(self.paths["state"]),
            "TMP": str(self.paths["state"]),
        }
        with patch(
            "pipeline.deployment._check_toolchain",
            return_value=self.toolchain_payload(),
        ), patch(
            "pipeline.deployment.tempfile.TemporaryDirectory",
            side_effect=recording_temporary_directory,
        ):
            report = check_offline(poisoned_environment)

        self.assertTrue(created_scratch)
        for scratch in created_scratch:
            self.assertEqual(scratch.parent, self.paths["scratch"].resolve())
            self.assertFalse(scratch.exists())
            self.assertFalse(
                scratch == self.paths["state"].resolve()
                or self.paths["state"].resolve() in scratch.parents
                or scratch in self.paths["state"].resolve().parents
            )
        self.assertEqual(_tree_state(self.paths["state"]), source_before)
        self.assertEqual(
            report["d1"]["scratchRoot"],
            str(self.paths["scratch"].resolve()),
        )
        self.assertEqual(list(self.paths["scratch"].iterdir()), [])

    def test_sqlite_scratch_rejects_source_overlap(self) -> None:
        self.ensure_generation()
        with patch(
            "pipeline.deployment.SYSTEMD_SQLITE_SCRATCH_ROOT",
            self.paths["state"],
        ), patch(
            "pipeline.deployment._check_toolchain",
            return_value=self.toolchain_payload(),
        ):
            with self.assertRaises(DeploymentCheckError) as raised:
                check_offline(self.environment)
        self.assertEqual(raised.exception.code, "sqlite_scratch_overlap")

    def test_sqlite_scratch_rejects_symlink(self) -> None:
        self.ensure_generation()
        linked = Path(self.temporary.name) / "sqlite-scratch-link"
        try:
            linked.symlink_to(self.paths["scratch"], target_is_directory=True)
        except OSError as error:
            self.skipTest(f"directory symlinks are unavailable: {error}")
        with patch(
            "pipeline.deployment.SYSTEMD_SQLITE_SCRATCH_ROOT",
            linked,
        ), patch(
            "pipeline.deployment._check_toolchain",
            return_value=self.toolchain_payload(),
        ):
            with self.assertRaises(DeploymentCheckError) as raised:
                check_offline(self.environment)
        self.assertEqual(raised.exception.code, "sqlite_scratch_unsafe")

    def test_systemd_profile_rejects_a_safe_but_noncanonical_mutable_path(self) -> None:
        self.ensure_generation()
        alternate = Path(self.temporary.name) / "alternate-runtime"
        alternate.mkdir()
        environment = {**self.environment, "RARDAR_RUNTIME_DIR": str(alternate)}
        with patch(
            "pipeline.deployment._check_toolchain",
            return_value=self.toolchain_payload(),
        ):
            with self.assertRaises(DeploymentCheckError) as raised:
                check_offline(environment)
        self.assertEqual(raised.exception.code, "deployment_layout_noncanonical")

    @unittest.skipIf(os.name == "nt", "POSIX mode ownership is a Linux deployment gate")
    def test_mutable_roots_reject_world_writable_mode(self) -> None:
        original_mode = stat.S_IMODE(self.paths["state"].stat().st_mode)
        try:
            for mode in (0o777, 0o500):
                with self.subTest(mode=oct(mode)):
                    self.paths["state"].chmod(mode)
                    with self.assertRaises(DeploymentCheckError) as raised:
                        check_offline(self.environment)
                    self.assertEqual(raised.exception.code, "deployment_path_unsafe_mode")
        finally:
            self.paths["state"].chmod(original_mode)

    @unittest.skipIf(os.name == "nt", "POSIX ownership is a Linux deployment gate")
    def test_mutable_roots_require_the_effective_service_owner(self) -> None:
        unexpected_owner = os.geteuid() + 1
        with patch("pipeline.deployment.os.geteuid", return_value=unexpected_owner):
            with self.assertRaises(DeploymentCheckError) as raised:
                check_offline(self.environment)
        self.assertEqual(raised.exception.code, "deployment_path_owner_mismatch")

    def test_corrupt_sqlite_fails_closed(self) -> None:
        database = self.paths["state"] / "d1" / "rardar.sqlite"
        original = database.read_bytes()
        database.write_bytes(original[:128])
        with self.assertRaises(DeploymentCheckError) as raised:
            _check_vinext_state(self.paths["state"])
        self.assertEqual(raised.exception.code, "sqlite_integrity_failed")

    def test_invalid_current_generation_fails_closed(self) -> None:
        self.ensure_generation()
        pointer = self.paths["data"] / "current.json"
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        payload["manifestSha256"] = "0" * 64
        pointer.write_text(json.dumps(payload), encoding="utf-8")
        with patch(
            "pipeline.deployment._check_toolchain",
            return_value=self.toolchain_payload(),
        ):
            with self.assertRaises(DeploymentCheckError) as raised:
                check_offline(self.environment)
        self.assertEqual(raised.exception.code, "published_generation_invalid")

    def test_toolchain_requires_node_22_13_and_the_running_python(self) -> None:
        completed = subprocess.CompletedProcess(
            [self.environment["RARDAR_NODE"], "--version"],
            0,
            stdout="v22.13.1\n",
            stderr="",
        )
        with patch("pipeline.deployment.subprocess.run", return_value=completed):
            report = _check_toolchain(self.environment)
        self.assertEqual(report["node"], "22.13.1")

        old = subprocess.CompletedProcess(
            [self.environment["RARDAR_NODE"], "--version"],
            0,
            stdout="v22.12.0\n",
            stderr="",
        )
        with patch("pipeline.deployment.subprocess.run", return_value=old):
            with self.assertRaises(DeploymentCheckError) as raised:
                _check_toolchain(self.environment)
        self.assertEqual(raised.exception.code, "node_version_unsupported")


class OnlineDeploymentTests(DeploymentFixture):
    def run_online(self, status: dict[str, object], health: dict[str, object]):
        self.ensure_generation()
        def json_response(port: int, path: str):
            if path == "/status" and port == 39102:
                return status
            if path == "/api/health" and port == 39101:
                return health
            raise AssertionError(f"unexpected JSON request: {port} {path}")

        with patch(
            "pipeline.deployment._check_toolchain",
            return_value=self.toolchain_payload(),
        ), patch(
            "pipeline.deployment.sys.platform", "linux"
        ), patch("pipeline.deployment._http_json", side_effect=json_response), patch(
            "pipeline.deployment._http_ok"
        ) as http_ok, patch(
            "pipeline.deployment._pid_is_alive", return_value=True
        ), patch(
            "pipeline.deployment._check_process_command"
        ), patch(
            "pipeline.deployment._check_loopback_listener",
            side_effect=lambda port, _pid: {"port": port, "addresses": ["127.0.0.1"]},
        ), patch(
            "pipeline.deployment._probe_lock_file",
            side_effect=AssertionError("online check must not demand idle locks"),
        ):
            report = check_online(self.environment)
        self.assertEqual(
            [call.args for call in http_ok.call_args_list],
            [(39101, "/"), (39101, "/signals"), (39101, "/search")],
        )
        return report

    def test_online_accepts_healthy_runtime_with_matching_generation(self) -> None:
        report = self.run_online(
            self.healthy_status(),
            self.healthy_health(),
        )
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["mode"], "online")
        self.assertEqual(report["locks"]["status"], "not_checked")
        self.assertEqual(report["http"]["generationId"], self.generation_id)

    def test_online_binds_runtime_layout_schedule_and_stale_threshold_to_environment(self) -> None:
        mutations = (
            (("runtime", "home"), str(Path(self.temporary.name) / "other-home")),
            (("runtime", "dataDir"), str(Path(self.temporary.name) / "other-data")),
            (("runtime", "runtimeDir"), str(Path(self.temporary.name) / "other-runtime")),
            (("runtime", "dataLockDir"), str(Path(self.temporary.name) / "other-locks")),
            (("runtime", "vinextPort"), 49101),
            (("runtime", "runtimeStatusPort"), 49102),
            (("schedule", "at"), "07:15"),
            (("schedule", "timezone"), "Asia/Shanghai"),
            (("data", "staleAfterSeconds"), 36 * 60 * 60),
        )
        for keys, value in mutations:
            with self.subTest(field=".".join(keys)):
                status = json.loads(json.dumps(self.healthy_status()))
                target = status
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
                with self.assertRaises(DeploymentCheckError) as raised:
                    self.run_online(status, self.healthy_health())
                self.assertEqual(raised.exception.code, "runtime_configuration_mismatch")

    def test_online_binds_health_schedule_and_stale_threshold_to_environment(self) -> None:
        for keys, value in (
            (("schedule", "at"), "07:15"),
            (("schedule", "timezone"), "Asia/Shanghai"),
            (("data", "staleAfterSeconds"), 36 * 60 * 60),
        ):
            with self.subTest(field=".".join(keys)):
                health = json.loads(json.dumps(self.healthy_health()))
                target = health
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
                with self.assertRaises(DeploymentCheckError) as raised:
                    self.run_online(self.healthy_status(), health)
                self.assertEqual(raised.exception.code, "runtime_configuration_mismatch")

    def test_online_accepts_only_explicit_stale_degradation(self) -> None:
        report = self.run_online(
            self.healthy_status(stale=True),
            self.healthy_health(stale=True),
        )
        self.assertEqual(report["http"]["health"], "degraded")
        self.assertEqual(report["http"]["reason"], "published_data_stale")

        with self.assertRaises(DeploymentCheckError) as raised:
            self.run_online(
                self.healthy_status(stale=True),
                {
                    "status": "degraded",
                    "reason": "published_generation_unavailable",
                    "generationId": self.generation_id,
                    "data": {"staleAfterSeconds": 48 * 60 * 60},
                    "schedule": {"at": "06:45", "timezone": "Europe/Berlin"},
                },
            )
        self.assertEqual(raised.exception.code, "website_health_invalid")

    def test_online_rejects_generation_mismatch(self) -> None:
        with self.assertRaises(DeploymentCheckError) as raised:
            self.run_online(
                self.healthy_status(),
                {
                    **self.healthy_health(),
                    "generationId": "forged-generation",
                },
            )
        self.assertEqual(raised.exception.code, "runtime_generation_mismatch")

    def test_online_rejects_colliding_ports_before_http(self) -> None:
        self.ensure_generation()
        environment = dict(self.environment)
        environment["RARDAR_RUNTIME_STATUS_PORT"] = environment["RARDAR_VINEXT_PORT"]
        with patch(
            "pipeline.deployment._check_toolchain",
            return_value=self.toolchain_payload(),
        ):
            with self.assertRaises(DeploymentCheckError) as raised:
                check_online(environment)
        self.assertEqual(raised.exception.code, "runtime_configuration_invalid")


class DeploymentCliAndUnitTests(unittest.TestCase):
    def test_cli_emits_stable_failure_envelope(self) -> None:
        output = io.StringIO()
        with patch(
            "pipeline.deployment.check_offline",
            side_effect=DeploymentCheckError("fixture_failed", "fixture detail"),
        ), redirect_stdout(output):
            exit_code = main(["check", "--offline"])
        payload = json.loads(output.getvalue())
        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error"]["code"], "fixture_failed")

    def test_systemd_unit_owns_only_the_foreground_manager(self) -> None:
        unit = SYSTEMD_UNIT.read_text(encoding="utf-8")
        self.assertIn("Type=simple", unit)
        self.assertIn("User=rardar", unit)
        self.assertIn("EnvironmentFile=/etc/rardar/rardar.env", unit)
        self.assertIn("WorkingDirectory=/opt/rardar/current", unit)
        self.assertIn(
            "ExecStartPre=/opt/rardar/current/.venv/bin/python -m "
            "pipeline.deployment check --offline",
            unit,
        )
        self.assertIn(
            "ExecStart=/opt/rardar/current/.venv/bin/python -m pipeline.runtime service",
            unit,
        )
        self.assertIn("KillMode=control-group", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("RestartPreventExitStatus=4", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertNotIn("pipeline.scheduler", unit)
        self.assertNotIn("local:start", unit)

        writable_line = next(
            line for line in unit.splitlines() if line.startswith("ReadWritePaths=")
        )
        writable_paths = set(writable_line.removeprefix("ReadWritePaths=").split())
        expected_writable_paths = {
            "/var/lib/rardar/data",
            "/var/lib/rardar/runtime",
            "/var/lib/rardar/vinext-state",
            "/var/lib/rardar/locks",
            "/var/cache/rardar/vite",
            "/var/log/rardar/wrangler",
            "/var/backups/rardar",
        }
        self.assertEqual(writable_paths, expected_writable_paths)

        example_values = {
            name: value
            for line in SYSTEMD_ENV.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#") and "=" in line
            for name, value in (line.split("=", 1),)
        }
        self.assertEqual(
            {name: Path(example_values[name]) for name in CANONICAL_SYSTEMD_PATHS},
            CANONICAL_SYSTEMD_PATHS,
        )
        for name in (
            "RARDAR_DATA_DIR",
            "RARDAR_RUNTIME_DIR",
            "RARDAR_VINEXT_STATE_DIR",
            "RARDAR_DATA_LOCK_DIR",
            "RARDAR_VITE_CACHE_DIR",
            "RARDAR_BACKUP_DIR",
            "WRANGLER_LOG_PATH",
        ):
            configured = CANONICAL_SYSTEMD_PATHS[name]
            self.assertTrue(
                any(configured == Path(root) or Path(root) in configured.parents for root in writable_paths),
                f"{name} is outside systemd ReadWritePaths",
            )

    def test_environment_example_is_loopback_and_has_no_secret(self) -> None:
        example = SYSTEMD_ENV.read_text(encoding="utf-8")
        for variable in (
            "RARDAR_HOME",
            "RARDAR_DATA_DIR",
            "RARDAR_RUNTIME_DIR",
            "RARDAR_VINEXT_STATE_DIR",
            "RARDAR_DATA_LOCK_DIR",
            "RARDAR_NODE",
            "RARDAR_PYTHON",
            "RARDAR_VINEXT_PORT=3000",
            "RARDAR_RUNTIME_STATUS_PORT=3002",
        ):
            self.assertIn(variable, example)
        self.assertNotIn("ghp_", example)
        self.assertNotRegex(
            example,
            r"(?m)^\s*(?:export\s+)?GITHUB_TOKEN\s*=",
        )
        self.assertNotIn("github_pat_", example)
        self.assertNotIn("0.0.0.0", example)


if __name__ == "__main__":
    unittest.main()
