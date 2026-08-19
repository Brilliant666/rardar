from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from pipeline.release_artifact import ReleaseArtifactError, verify_release_root
from pipeline.release_manifest import create_release_manifest
from pipeline.release_package import accept_archive, create_archive, stage_release


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPOSITORY_ROOT / ".github" / "workflows" / "release-artifact.yml"
TEST_SHA = "a" * 40


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_release_fixture(root: Path) -> None:
    root.mkdir()
    files = (
        "package.json",
        "package-lock.json",
        "requirements.lock",
        "pipeline/runtime.py",
        "pipeline/deployment.py",
        "pipeline/release_artifact.py",
        "node_modules/vite/bin/vite.js",
        "node_modules/vinext/dist/cli.js",
        "vite.config.ts",
        ".openai/hosting.json",
        "app/runtime-readiness.mjs",
        "build/published-data-bridge.ts",
        "build/sites-vite-plugin.ts",
        "worker/index.ts",
        "deploy/systemd/rardar.service",
    )
    for relative in files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"fixture:{relative}\n", encoding="utf-8")
    (root / "requirements.lock").write_text(
        "fixture-alpha==1.0.0\nfixture.beta==2.0.0\n",
        encoding="utf-8",
    )
    (root / "dist").mkdir()
    (root / "deploy" / "systemd").mkdir(parents=True, exist_ok=True)
    wheelhouse = root / "wheelhouse"
    wheelhouse.mkdir()
    (wheelhouse / "fixture_alpha-1.0.0-py3-none-any.whl").write_bytes(b"alpha")
    (wheelhouse / "fixture_beta-2.0.0-py3-none-any.whl").write_bytes(b"beta")
    create_release_manifest(
        root,
        commit_sha=TEST_SHA,
        verify_workflow_run_id="123456",
        verify_workflow_head_sha=TEST_SHA,
        npm_version="10.9.2",
        built_at="2026-08-18T00:00:00Z",
    )


def _tree_snapshot(root: Path) -> dict[str, tuple[str, bytes | str | None]]:
    state: dict[str, tuple[str, bytes | str | None]] = {}
    for path in (root, *sorted(root.rglob("*"))):
        relative = "." if path == root else path.relative_to(root).as_posix()
        if path.is_symlink():
            state[relative] = ("symlink", os.readlink(path))
        elif path.is_file():
            state[relative] = ("file", path.read_bytes())
        elif path.is_dir():
            state[relative] = ("directory", None)
        else:
            state[relative] = ("other", None)
    return state


def _rewrite_manifest(root: Path, **changes: object) -> None:
    path = root / "release-manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(changes)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_minimal_wheel(wheelhouse: Path) -> Path:
    wheelhouse.mkdir()
    wheel = wheelhouse / "rardar_offline_fixture-1.0.0-py3-none-any.whl"
    package = "rardar_offline_fixture/__init__.py"
    info = "rardar_offline_fixture-1.0.0.dist-info"
    metadata = "Metadata-Version: 2.1\nName: rardar-offline-fixture\nVersion: 1.0.0\n"
    wheel_metadata = (
        "Wheel-Version: 1.0\n"
        "Generator: rardar-test\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    )
    record = (
        f"{package},,\n"
        f"{info}/METADATA,,\n"
        f"{info}/WHEEL,,\n"
        f"{info}/RECORD,,\n"
    )
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(package, "VALUE = 'offline'\n")
        archive.writestr(f"{info}/METADATA", metadata)
        archive.writestr(f"{info}/WHEEL", wheel_metadata)
        archive.writestr(f"{info}/RECORD", record)
    return wheel


class ReleaseArtifactTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / TEST_SHA
        _write_release_fixture(self.root)

    def test_valid_exact_release_is_read_only(self) -> None:
        before = _tree_snapshot(self.root)
        report = verify_release_root(self.root, expected_sha=TEST_SHA)
        after = _tree_snapshot(self.root)
        self.assertEqual(report["status"], "healthy")
        self.assertEqual(report["commitSha"], TEST_SHA)
        self.assertEqual(report["wheelCount"], 2)
        self.assertEqual(report["requirementCount"], 2)
        self.assertEqual(before, after)

    def test_wrong_or_short_expected_sha_fails(self) -> None:
        for expected in ("b" * 40, "a" * 12):
            with self.subTest(expected=expected):
                with self.assertRaises(ReleaseArtifactError) as raised:
                    verify_release_root(self.root, expected_sha=expected)
                self.assertEqual(raised.exception.code, "FAIL_RELEASE_ARTIFACT_IDENTITY")

    def test_manifest_rejects_short_sha_wrong_platform_and_wrong_architecture(self) -> None:
        cases = (
            ({"commitSha": "a" * 12}, "FAIL_RELEASE_ARTIFACT_IDENTITY"),
            ({"platform": "windows"}, "FAIL_RELEASE_ARTIFACT_MANIFEST"),
            ({"architecture": "aarch64"}, "FAIL_RELEASE_ARTIFACT_MANIFEST"),
        )
        original = (self.root / "release-manifest.json").read_bytes()
        for changes, code in cases:
            with self.subTest(changes=changes):
                (self.root / "release-manifest.json").write_bytes(original)
                _rewrite_manifest(self.root, **changes)
                with self.assertRaises(ReleaseArtifactError) as raised:
                    verify_release_root(self.root, expected_sha=TEST_SHA)
                self.assertEqual(raised.exception.code, code)

    def test_lock_mismatch_fails(self) -> None:
        for relative in ("package-lock.json", "requirements.lock"):
            with (
                self.subTest(relative=relative),
                tempfile.TemporaryDirectory() as temporary,
            ):
                fixture = Path(temporary) / TEST_SHA
                shutil.copytree(self.root, fixture)
                (fixture / relative).write_text("changed\n", encoding="utf-8")
                with self.assertRaises(ReleaseArtifactError) as raised:
                    verify_release_root(fixture, expected_sha=TEST_SHA)
                self.assertEqual(
                    raised.exception.code,
                    "FAIL_RELEASE_ARTIFACT_LOCK_MISMATCH",
                )

    def test_required_node_and_build_content_is_fail_closed(self) -> None:
        targets = (
            "node_modules/vite/bin/vite.js",
            "node_modules/vinext/dist/cli.js",
            "dist",
        )
        for relative in targets:
            with self.subTest(relative=relative):
                with tempfile.TemporaryDirectory() as temporary:
                    fixture = Path(temporary) / TEST_SHA
                    shutil.copytree(self.root, fixture)
                    target = fixture / relative
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                    with self.assertRaises(ReleaseArtifactError) as raised:
                        verify_release_root(fixture, expected_sha=TEST_SHA)
                    self.assertEqual(raised.exception.code, "FAIL_RELEASE_ARTIFACT_INCOMPLETE")

    def test_data_and_secret_files_are_forbidden(self) -> None:
        candidates = (
            self.root / "data" / "current.json",
            self.root / ".env",
            self.root / ".dev.vars.production",
            self.root / "deploy" / "rardar.secret",
            self.root / "nested" / "credentials.json",
            self.root / "nested" / "tokens" / "entry.json",
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate.name):
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_text("secret\n", encoding="utf-8")
                with self.assertRaises(ReleaseArtifactError) as raised:
                    verify_release_root(self.root, expected_sha=TEST_SHA)
                self.assertEqual(raised.exception.code, "FAIL_RELEASE_ARTIFACT_FORBIDDEN_CONTENT")
                candidate.unlink()
                if candidate.parent.name == "data":
                    candidate.parent.rmdir()

    def test_raw_artifact_rejects_a_packaged_virtual_environment(self) -> None:
        (self.root / ".venv").mkdir()
        (self.root / ".venv" / "pyvenv.cfg").write_text("fixture\n", encoding="utf-8")
        with self.assertRaises(ReleaseArtifactError) as raised:
            verify_release_root(self.root, expected_sha=TEST_SHA)
        self.assertEqual(
            raised.exception.code,
            "FAIL_RELEASE_ARTIFACT_FORBIDDEN_CONTENT",
        )

    def test_installed_release_mode_allows_only_a_real_top_level_virtual_environment(self) -> None:
        venv = self.root / ".venv"
        venv.mkdir()
        (venv / "pyvenv.cfg").write_text("fixture\n", encoding="utf-8")
        report = verify_release_root(
            self.root,
            expected_sha=TEST_SHA,
            allow_runtime_venv=True,
        )
        self.assertEqual(report["status"], "healthy")

        shutil.rmtree(venv)
        outside = Path(self.temporary.name) / "outside-venv"
        outside.mkdir()
        try:
            venv.symlink_to(outside, target_is_directory=True)
        except OSError as error:
            self.skipTest(f"directory symlinks are unavailable: {error}")
        with self.assertRaises(ReleaseArtifactError) as raised:
            verify_release_root(
                self.root,
                expected_sha=TEST_SHA,
                allow_runtime_venv=True,
            )
        self.assertEqual(raised.exception.code, "FAIL_RELEASE_ARTIFACT_FORBIDDEN_CONTENT")

    def test_safe_relative_node_bin_symlink_is_allowed(self) -> None:
        link = self.root / "node_modules" / ".bin" / "vite"
        link.parent.mkdir()
        try:
            link.symlink_to("../vite/bin/vite.js")
        except OSError as error:
            self.skipTest(f"file symlinks are unavailable: {error}")
        report = verify_release_root(self.root, expected_sha=TEST_SHA)
        self.assertEqual(report["symlinks"], 1)

    def test_absolute_and_escape_symlinks_fail(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        outside.write_text("outside\n", encoding="utf-8")
        link = self.root / "node_modules" / ".bin" / "unsafe"
        link.parent.mkdir()
        for target in (outside, Path("..") / ".." / ".." / "outside"):
            with self.subTest(target=str(target)):
                try:
                    link.symlink_to(target)
                except OSError as error:
                    self.skipTest(f"file symlinks are unavailable: {error}")
                with self.assertRaises(ReleaseArtifactError) as raised:
                    verify_release_root(self.root, expected_sha=TEST_SHA)
                self.assertEqual(raised.exception.code, "FAIL_RELEASE_ARTIFACT_UNSAFE_LINK")
                link.unlink()

    def test_missing_relative_symlink_target_fails(self) -> None:
        link = self.root / "node_modules" / ".bin" / "missing"
        link.parent.mkdir()
        try:
            link.symlink_to("../missing-package/cli.js")
        except OSError as error:
            self.skipTest(f"file symlinks are unavailable: {error}")
        with self.assertRaises(ReleaseArtifactError) as raised:
            verify_release_root(self.root, expected_sha=TEST_SHA)
        self.assertEqual(raised.exception.code, "FAIL_RELEASE_ARTIFACT_UNSAFE_LINK")

    def test_missing_or_incomplete_wheelhouse_fails(self) -> None:
        shutil.rmtree(self.root / "wheelhouse")
        with self.assertRaises(ReleaseArtifactError) as raised:
            verify_release_root(self.root, expected_sha=TEST_SHA)
        self.assertEqual(raised.exception.code, "FAIL_RELEASE_ARTIFACT_INCOMPLETE")
        (self.root / "wheelhouse").mkdir()
        (self.root / "wheelhouse" / "fixture_alpha-1.0.0-py3-none-any.whl").write_bytes(b"alpha")
        with self.assertRaises(ReleaseArtifactError) as raised:
            verify_release_root(self.root, expected_sha=TEST_SHA)
        self.assertEqual(raised.exception.code, "FAIL_RELEASE_ARTIFACT_WHEELHOUSE")

    def test_manifest_creation_is_create_only(self) -> None:
        manifest = self.root / "release-manifest.json"
        manifest.unlink()
        payload = create_release_manifest(
            self.root,
            commit_sha=TEST_SHA,
            verify_workflow_run_id="987",
            verify_workflow_head_sha=TEST_SHA,
            npm_version="10.9.2",
            built_at="2026-08-18T08:00:00+08:00",
        )
        self.assertEqual(payload["builtAt"], "2026-08-18T00:00:00Z")
        with self.assertRaises(ReleaseArtifactError) as raised:
            create_release_manifest(
                self.root,
                commit_sha=TEST_SHA,
                verify_workflow_run_id="987",
                verify_workflow_head_sha=TEST_SHA,
                npm_version="10.9.2",
                built_at="2026-08-18T00:00:00Z",
            )
        self.assertEqual(raised.exception.code, "FAIL_RELEASE_ARTIFACT_MANIFEST")

    def test_stage_uses_exact_git_archive_and_excludes_tracked_data(self) -> None:
        source = Path(self.temporary.name) / "source"
        shutil.copytree(self.root, source)
        (source / "release-manifest.json").unlink()
        built = {
            name: source / name
            for name in ("node_modules", "dist", "wheelhouse")
        }
        preserved = Path(self.temporary.name) / "builder-output"
        preserved.mkdir()
        for name, path in built.items():
            shutil.move(str(path), preserved / name)
        (source / "data").mkdir()
        (source / "data" / "current.json").write_text('{"tracked":"must-not-ship"}\n', encoding="utf-8")
        commands = (
            ("init",),
            ("config", "user.email", "release-test@example.invalid"),
            ("config", "user.name", "Release Test"),
            ("add", "."),
            ("commit", "-m", "fixture"),
        )
        for command in commands:
            completed = subprocess.run(
                ["git", "-C", str(source), *command],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        commit_sha = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            text=True,
        ).strip()
        for name in built:
            shutil.move(str(preserved / name), source / name)
        stage = Path(self.temporary.name) / "stage"
        report = stage_release(
            source,
            stage,
            commit_sha=commit_sha,
            verify_workflow_run_id="777",
            verify_workflow_head_sha=commit_sha,
            npm_version="10.9.2",
            built_at="2026-08-18T00:00:00Z",
        )
        self.assertEqual(report["commitSha"], commit_sha)
        self.assertFalse((stage / "data").exists())
        self.assertTrue((source / "data" / "current.json").is_file())
        self.assertTrue((stage / "node_modules" / "vite" / "bin" / "vite.js").is_file())

    def test_archive_is_deterministic_and_fresh_extraction_is_verified(self) -> None:
        output = Path(self.temporary.name) / "output"
        output.mkdir()
        first = output / "first.tar.gz"
        second = output / "second.tar.gz"
        first_report = create_archive(
            self.root,
            first,
            expected_sha=TEST_SHA,
            source_date_epoch=1_700_000_000,
        )
        second_report = create_archive(
            self.root,
            second,
            expected_sha=TEST_SHA,
            source_date_epoch=1_700_000_000,
        )
        self.assertEqual(first_report["sha256"], second_report["sha256"])
        accepted = accept_archive(
            first,
            Path(first_report["checksum"]),
            Path(self.temporary.name) / "accepted",
            expected_sha=TEST_SHA,
        )
        self.assertEqual(accepted["commitSha"], TEST_SHA)
        self.assertEqual(accepted["archiveSha256"], first_report["sha256"])

    def test_checksum_mismatch_fails_before_extraction(self) -> None:
        archive = Path(self.temporary.name) / "release.tar.gz"
        report = create_archive(
            self.root,
            archive,
            expected_sha=TEST_SHA,
            source_date_epoch=1_700_000_000,
        )
        archive.write_bytes(archive.read_bytes() + b"tamper")
        target = Path(self.temporary.name) / "must-not-exist"
        with self.assertRaises(ReleaseArtifactError) as raised:
            accept_archive(
                archive,
                Path(report["checksum"]),
                target,
                expected_sha=TEST_SHA,
            )
        self.assertEqual(raised.exception.code, "FAIL_RELEASE_ARTIFACT_ARCHIVE")
        self.assertFalse(target.exists())

    def test_archive_absolute_and_escape_links_fail_before_extraction(self) -> None:
        for index, linkname in enumerate(("/etc/passwd", "../../../outside")):
            with self.subTest(linkname=linkname):
                archive = Path(self.temporary.name) / f"unsafe-{index}.tar.gz"
                with tarfile.open(archive, "w:gz") as target:
                    link = tarfile.TarInfo("node_modules/.bin/unsafe")
                    link.type = tarfile.SYMTYPE
                    link.linkname = linkname
                    target.addfile(link)
                checksum = archive.with_name(archive.name + ".sha256")
                checksum.write_text(f"{_sha256(archive)}  {archive.name}\n", encoding="ascii")
                extract = Path(self.temporary.name) / f"unsafe-extract-{index}"
                with self.assertRaises(ReleaseArtifactError) as raised:
                    accept_archive(
                        archive,
                        checksum,
                        extract,
                        expected_sha=TEST_SHA,
                    )
                self.assertEqual(raised.exception.code, "FAIL_RELEASE_ARTIFACT_UNSAFE_LINK")
                self.assertFalse(extract.exists())

    def test_offline_venv_install_fixture(self) -> None:
        wheelhouse = Path(self.temporary.name) / "offline-wheelhouse"
        _write_minimal_wheel(wheelhouse)
        requirements = Path(self.temporary.name) / "offline-requirements.lock"
        requirements.write_text("rardar-offline-fixture==1.0.0\n", encoding="utf-8")
        venv = Path(self.temporary.name) / "offline-venv"
        completed = subprocess.run(
            [sys.executable, "-m", "venv", str(venv)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        install = subprocess.run(
            [
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-index",
                "--find-links",
                str(wheelhouse),
                "--requirement",
                str(requirements),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
        checked = subprocess.run(
            [str(python), "-m", "pip", "check"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)


class ReleaseWorkflowContractTests(unittest.TestCase):
    def test_release_workflow_is_exact_main_verify_gated(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("workflow_run:", workflow)
        self.assertIn("workflows: [Verify]", workflow)
        self.assertIn("types: [completed]", workflow)
        self.assertIn("workflow_run.conclusion == 'success'", workflow)
        self.assertIn("workflow_run.event == 'push'", workflow)
        self.assertIn("workflow_run.head_branch == 'main'", workflow)
        self.assertIn("workflow_run.head_repository.full_name == github.repository", workflow)
        self.assertIn("ref: ${{ github.event.workflow_run.head_sha }}", workflow)
        self.assertNotIn("pull_request:", workflow)
        self.assertNotIn("workflow_dispatch:", workflow)

    def test_release_workflow_pins_platform_toolchains_and_actions(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("runs-on: ubuntu-24.04", workflow)
        self.assertNotIn("ubuntu-latest", workflow)
        self.assertIn("node-version: 22.13.1", workflow)
        self.assertIn('python-version: "3.12"', workflow)
        self.assertIn("actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0", workflow)
        self.assertIn("actions/setup-node@820762786026740c76f36085b0efc47a31fe5020", workflow)
        self.assertIn("actions/setup-python@ece7cb06caefa5fff74198d8649806c4678c61a1", workflow)
        self.assertIn("actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02", workflow)

    def test_release_workflow_uses_runner_temp_only_inside_steps(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        job_environment = workflow.split("    env:\n", 1)[1].split("\n\n    steps:", 1)[0]
        self.assertNotIn("runner.temp", job_environment)
        self.assertIn("$RUNNER_TEMP/rardar-release-stage", workflow)
        self.assertIn("$RUNNER_TEMP/rardar-release-output", workflow)
        self.assertIn("$RUNNER_TEMP/rardar-release-accept", workflow)
        self.assertIn("$RUNNER_TEMP/rardar-release-venv", workflow)

    def test_release_workflow_builds_and_accepts_without_server_side_npm(self) -> None:
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("npm ci", workflow)
        self.assertIn("npm run verify", workflow)
        self.assertIn("npm run build", workflow)
        self.assertIn("pip wheel", workflow)
        self.assertIn("pipeline.release_package build", workflow)
        self.assertIn("pipeline.release_package accept", workflow)
        self.assertIn("--no-index", workflow)
        acceptance = workflow.split("- name: Accept fresh extraction without npm", 1)[1].split(
            "- name: Record release identity",
            1,
        )[0]
        self.assertNotIn("npm ci", acceptance)
        self.assertNotIn("npm install", acceptance)
        self.assertIn("retention-days: 10", workflow)


if __name__ == "__main__":
    unittest.main()
