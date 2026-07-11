from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pipeline.generations import CurrentGenerationError
from pipeline.audit_data import audit_data
from pipeline.refresh import _write_json_batch
from pipeline.schema_validation import (
    ArtifactKind,
    ArtifactValidationError,
    _validate_cli_data_tree,
    atomic_write_validated_json,
    infer_artifact_kind,
    load_validated_json,
    require_valid,
    strict_json_loads,
    validate_data_tree,
    validate_payload,
)


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent


def valid_project_enrichment() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "repository": "demo/tool",
        "sourcePushedAt": "2026-07-10T23:00:00Z",
        "sourceAnalysisAt": "2026-07-10T23:30:00+00:00",
        "analyzedAt": "2026-07-11T00:00:00Z",
        "titleZh": "演示工具",
        "summaryZh": "用于验证 Rardar 数据契约。",
        "category": "开发工具",
        "capabilities": ["契约验证"],
        "taskTerms": ["schema", "audit"],
        "bestFor": "需要验证结构化数据的项目",
        "reusePlan": "先核对静态证据，再决定是否复用。",
        "limitation": "没有执行第三方仓库代码。",
        "evidenceSummary": "依据测试中的受控样例。",
        "sourceUrl": "https://github.com/demo/tool#readme",
    }


def valid_catalog() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "capturedAt": "2026-07-11T00:00:00Z",
        "sourceCount": 1,
        "queryFailureCount": 0,
        "projectCount": 1,
        "deepAnalysisCount": 0,
        "pendingDeepAnalysis": ["demo/tool"],
        "dailyTrackCounts": {"recentMomentum": 1, "longTerm": 0},
        "heatHistory": {
            "snapshotCount": 2,
            "maximumSnapshotCount": 30,
            "minimumPersistenceSnapshots": 7,
            "verifiedLongTermCount": 0,
        },
        "growthMode": "observed",
        "notice": "受控测试目录。",
        "projects": [
            {
                "slug": "demo--tool",
                "repo": "demo/tool",
                "title": "演示工具",
                "description": "用于测试数据契约。",
                "category": "开发工具",
                "language": "Python",
                "license": "MIT",
                "stars": 10,
                "growthValue": 2,
                "growthLabel": "观测 +2 / 24 小时",
                "growthKind": "observed",
                "globalScore": 70,
                "reuseScore": 60,
                "momentumScore": 75,
                "enduranceScore": 40,
                "heatTrack": "recent_momentum",
                "heatLabel": "近期动量 · 区间上升",
                "longTermEvidenceKind": None,
                "heatObservationCount": 2,
                "heatObservationWindow": 2,
                "trend": "+2 / 24h",
                "analysisState": "事实初筛",
                "sourcePushedAt": "2026-07-10T00:00:00Z",
                "analysisAnalyzedAt": None,
                "enrichmentAnalyzedAt": None,
                "whyNow": "两次快照之间观测到 Star 增长。",
                "recommendation": "收藏",
                "fit": "适合验证结构化数据。",
                "reusePlan": "先检查证据。",
                "risk": "尚未运行第三方代码。",
                "capabilities": ["契约验证"],
                "taskTerms": ["schema"],
                "evidence": [
                    {
                        "label": "GitHub",
                        "detail": "受控测试证据",
                        "href": "https://github.com/demo/tool",
                    }
                ],
                "capturedAt": "2026-07-11 08:00 CST",
            }
        ],
    }


def valid_generation_manifest() -> dict[str, object]:
    artifacts = ["catalog/latest.json", "snapshots/latest.json"]
    return {
        "schemaVersion": 1,
        "generationId": "gen-20260712-001",
        "createdAt": "2026-07-12T12:00:00+08:00",
        "baseGenerationId": "gen-20260711-001",
        "operation": "refresh",
        "state": "ready",
        "failureStage": None,
        "error": None,
        "artifacts": artifacts,
        "hashes": {path: "a" * 64 for path in artifacts},
        "audit": {
            "status": "healthy",
            "errorCount": 0,
            "warningCount": 0,
            "validatedCount": len(artifacts),
        },
    }


def valid_current_generation() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "generationId": "gen-20260712-001",
        "publishedAt": "2026-07-12T12:01:00+08:00",
        "previousGenerationId": "gen-20260711-001",
        "manifestSha256": "b" * 64,
    }


class SchemaValidationTests(unittest.TestCase):
    def test_accepts_generation_manifest_and_current_pointer_contracts(self) -> None:
        self.assertTrue(
            validate_payload(
                ArtifactKind.GENERATION_MANIFEST,
                valid_generation_manifest(),
            ).valid
        )
        self.assertTrue(
            validate_payload(
                ArtifactKind.CURRENT_GENERATION,
                valid_current_generation(),
            ).valid
        )

    def test_generation_contracts_reject_unknown_versions_and_timezone_less_times(self) -> None:
        cases = (
            (
                ArtifactKind.GENERATION_MANIFEST,
                valid_generation_manifest(),
                "createdAt",
            ),
            (
                ArtifactKind.CURRENT_GENERATION,
                valid_current_generation(),
                "publishedAt",
            ),
        )
        for kind, base, time_field in cases:
            with self.subTest(kind=kind.value, case="unknown version"):
                payload = deepcopy(base)
                payload["schemaVersion"] = 2
                self.assertFalse(validate_payload(kind, payload).valid)
            with self.subTest(kind=kind.value, case="timezone missing"):
                payload = deepcopy(base)
                payload[time_field] = "2026-07-12T12:00:00"
                self.assertFalse(validate_payload(kind, payload).valid)

    def test_generation_contracts_reject_unsafe_identifiers_and_artifact_paths(self) -> None:
        for generation_id in ("../escape", ".hidden", "trailing-", "has space"):
            with self.subTest(generation_id=generation_id):
                manifest = valid_generation_manifest()
                manifest["generationId"] = generation_id
                pointer = valid_current_generation()
                pointer["generationId"] = generation_id
                self.assertFalse(
                    validate_payload(ArtifactKind.GENERATION_MANIFEST, manifest).valid
                )
                self.assertFalse(
                    validate_payload(ArtifactKind.CURRENT_GENERATION, pointer).valid
                )

        for unsafe_path in (
            "../catalog/latest.json",
            "/catalog/latest.json",
            "catalog\\latest.json",
            "catalog//latest.json",
            "catalog/latest.txt",
        ):
            with self.subTest(path=unsafe_path):
                manifest = valid_generation_manifest()
                manifest["artifacts"] = [unsafe_path]
                manifest["hashes"] = {unsafe_path: "a" * 64}
                self.assertFalse(
                    validate_payload(ArtifactKind.GENERATION_MANIFEST, manifest).valid
                )

    def test_generation_manifest_rejects_duplicate_artifacts_and_invalid_hashes(self) -> None:
        duplicate = valid_generation_manifest()
        duplicate["artifacts"] = ["catalog/latest.json", "catalog/latest.json"]
        self.assertFalse(
            validate_payload(ArtifactKind.GENERATION_MANIFEST, duplicate).valid
        )

        invalid_digest = valid_generation_manifest()
        invalid_digest["hashes"]["catalog/latest.json"] = "A" * 64  # type: ignore[index]
        self.assertFalse(
            validate_payload(ArtifactKind.GENERATION_MANIFEST, invalid_digest).valid
        )

        mismatched = valid_generation_manifest()
        mismatched["hashes"].pop("catalog/latest.json")  # type: ignore[union-attr]
        result = validate_payload(ArtifactKind.GENERATION_MANIFEST, mismatched)
        self.assertFalse(result.valid)
        self.assertIn("/identity/artifact-hashes", {issue.schema_path for issue in result.issues})

        pointer = valid_current_generation()
        pointer["manifestSha256"] = "b" * 63
        self.assertFalse(
            validate_payload(ArtifactKind.CURRENT_GENERATION, pointer).valid
        )

    def test_generation_manifest_enforces_state_specific_diagnostics(self) -> None:
        building = valid_generation_manifest()
        building.update(
            {
                "state": "building",
                "audit": None,
                "failureStage": None,
                "error": None,
            }
        )
        self.assertTrue(
            validate_payload(ArtifactKind.GENERATION_MANIFEST, building).valid
        )

        empty_building = deepcopy(building)
        empty_building["artifacts"] = []
        empty_building["hashes"] = {}
        self.assertTrue(
            validate_payload(ArtifactKind.GENERATION_MANIFEST, empty_building).valid
        )

        failed = valid_generation_manifest()
        failed.update(
            {
                "state": "failed",
                "audit": None,
                "failureStage": "audit",
                "error": "catalog star count differs from snapshot",
            }
        )
        self.assertTrue(validate_payload(ArtifactKind.GENERATION_MANIFEST, failed).valid)

        empty_failed = deepcopy(failed)
        empty_failed["artifacts"] = []
        empty_failed["hashes"] = {}
        self.assertTrue(
            validate_payload(ArtifactKind.GENERATION_MANIFEST, empty_failed).valid
        )

        invalid_states = []
        ready_without_audit = valid_generation_manifest()
        ready_without_audit["audit"] = None
        invalid_states.append(ready_without_audit)
        ready_without_artifacts = valid_generation_manifest()
        ready_without_artifacts["artifacts"] = []
        ready_without_artifacts["hashes"] = {}
        invalid_states.append(ready_without_artifacts)
        failed_without_error = deepcopy(failed)
        failed_without_error["error"] = None
        invalid_states.append(failed_without_error)
        building_with_audit = deepcopy(building)
        building_with_audit["audit"] = valid_generation_manifest()["audit"]
        invalid_states.append(building_with_audit)

        for index, payload in enumerate(invalid_states):
            with self.subTest(case=index):
                self.assertFalse(
                    validate_payload(ArtifactKind.GENERATION_MANIFEST, payload).valid
                )

    def test_cli_validation_keeps_legacy_mode_only_when_pointer_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "legacy"
            data_dir.mkdir()
            expected: list = []
            with patch(
                "pipeline.schema_validation.validate_data_tree",
                return_value=expected,
            ) as validate_tree:
                results = _validate_cli_data_tree(data_dir)

        self.assertIs(results, expected)
        validate_tree.assert_called_once_with(data_dir)

    def test_cli_validation_resolves_pointer_once_without_recursive_audit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            immutable_root = data_dir / "generations/gen-001"
            immutable_root.mkdir(parents=True)
            (data_dir / "current.json").write_text("{}", encoding="utf-8")
            expected: list = []
            with (
                patch(
                    "pipeline.generations.resolve_current_generation",
                    return_value=SimpleNamespace(root=immutable_root, legacy=False),
                ) as resolve,
                patch(
                    "pipeline.schema_validation.validate_data_tree",
                    return_value=expected,
                ) as validate_tree,
            ):
                results = _validate_cli_data_tree(data_dir)

        self.assertIs(results, expected)
        resolve.assert_called_once_with(data_dir, verify_audit=False)
        validate_tree.assert_called_once_with(immutable_root)

    def test_cli_validation_reports_pointer_resolution_failures_structurally(self) -> None:
        failure_codes = (
            "current_pointer_invalid",
            "current_generation_missing",
            "manifest_digest_mismatch",
        )
        for code in failure_codes:
            with self.subTest(code=code):
                with tempfile.TemporaryDirectory() as directory:
                    data_dir = Path(directory) / "data"
                    data_dir.mkdir()
                    pointer = data_dir / "current.json"
                    pointer.write_text("{}", encoding="utf-8")
                    error = CurrentGenerationError(
                        code,
                        "controlled current generation failure",
                        stage="resolve",
                    )
                    with (
                        patch(
                            "pipeline.generations.resolve_current_generation",
                            side_effect=error,
                        ),
                        patch(
                            "pipeline.schema_validation.validate_data_tree"
                        ) as validate_tree,
                    ):
                        results = _validate_cli_data_tree(data_dir)

                self.assertEqual(len(results), 1)
                self.assertEqual(results[0].kind, ArtifactKind.CURRENT_GENERATION)
                self.assertFalse(results[0].valid)
                self.assertEqual(results[0].issues[0].schema_path, f"/resolution/{code}")
                self.assertIn(code, results[0].issues[0].message)
                validate_tree.assert_not_called()

    def test_cli_validation_reports_schema_errors_from_resolved_immutable_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            immutable_root = data_dir / "generations/gen-001"
            catalog_path = immutable_root / "catalog/latest.json"
            catalog_path.parent.mkdir(parents=True)
            catalog_path.write_text('{"schemaVersion": 1}', encoding="utf-8")
            (data_dir / "current.json").write_text("{}", encoding="utf-8")
            with patch(
                "pipeline.generations.resolve_current_generation",
                return_value=SimpleNamespace(root=immutable_root, legacy=False),
            ):
                results = _validate_cli_data_tree(data_dir)

        catalog_results = [
            result for result in results if result.kind is ArtifactKind.CATALOG
        ]
        self.assertEqual(len(catalog_results), 1)
        self.assertFalse(catalog_results[0].valid)
        self.assertEqual(
            Path(str(catalog_results[0].issues[0].source_path)).resolve(),
            catalog_path.resolve(),
        )

    def test_cli_validation_never_falls_back_when_pointer_path_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            data_dir.mkdir()
            (data_dir / "current.json").write_text("{}", encoding="utf-8")
            with (
                patch(
                    "pipeline.generations.resolve_current_generation",
                    return_value=SimpleNamespace(root=data_dir, legacy=True),
                ),
                patch("pipeline.schema_validation.validate_data_tree") as validate_tree,
            ):
                results = _validate_cli_data_tree(data_dir)

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].valid)
        self.assertEqual(
            results[0].issues[0].schema_path,
            "/resolution/current_pointer_invalid",
        )
        validate_tree.assert_not_called()

    def test_infers_generation_contract_paths_and_validates_direct_candidate_manifest(self) -> None:
        self.assertEqual(
            infer_artifact_kind(Path("data/current.json")),
            ArtifactKind.CURRENT_GENERATION,
        )
        self.assertEqual(
            infer_artifact_kind(Path("data/generations/gen-001/manifest.json")),
            ArtifactKind.GENERATION_MANIFEST,
        )
        self.assertEqual(
            infer_artifact_kind(
                Path("data/generations/.candidates/gen-001/manifest.json")
            ),
            ArtifactKind.GENERATION_MANIFEST,
        )

        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "data/generations/.candidates/gen-20260712-001"
            candidate.mkdir(parents=True)
            manifest = valid_generation_manifest()
            (candidate / "manifest.json").write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )

            results = validate_data_tree(candidate)

        manifest_results = [
            result for result in results if result.kind is ArtifactKind.GENERATION_MANIFEST
        ]
        self.assertEqual(len(manifest_results), 1)
        self.assertTrue(manifest_results[0].valid, manifest_results[0].issues)

    def test_accepts_valid_enrichment_without_mutating_it(self) -> None:
        payload = valid_project_enrichment()
        before = deepcopy(payload)

        validated = require_valid(ArtifactKind.PROJECT_ENRICHMENT, payload)

        self.assertIs(validated, payload)
        self.assertEqual(payload, before)

    def test_rejects_invalid_enrichment_types_formats_version_and_lengths(self) -> None:
        cases: dict[str, tuple[str, object]] = {
            "capabilities must be an array": ("capabilities", "not-an-array"),
            "taskTerms members must be strings": ("taskTerms", ["schema", 7]),
            "sourceUrl must be HTTP(S)": ("sourceUrl", "javascript:alert(1)"),
            "analyzedAt must be RFC3339": ("analyzedAt", "yesterday"),
            "schemaVersion must be known": ("schemaVersion", 99),
            "titleZh has a length limit": ("titleZh", "x" * 501),
        }

        for label, (field, value) in cases.items():
            with self.subTest(label=label):
                payload = valid_project_enrichment()
                payload[field] = value
                result = validate_payload(ArtifactKind.PROJECT_ENRICHMENT, payload)
                self.assertFalse(result.valid)
                self.assertTrue(
                    any(issue.instance_path.startswith(f"/{field}") for issue in result.issues),
                    result.issues,
                )

    def test_project_enrichment_v1_requires_evidence_source_times(self) -> None:
        for field in ("sourcePushedAt", "sourceAnalysisAt"):
            with self.subTest(field=field, case="missing"):
                payload = valid_project_enrichment()
                payload.pop(field)
                result = validate_payload(ArtifactKind.PROJECT_ENRICHMENT, payload)
                self.assertFalse(result.valid)
                self.assertTrue(
                    any(field in issue.message for issue in result.issues),
                    result.issues,
                )
            with self.subTest(field=field, case="wrong type"):
                payload = valid_project_enrichment()
                payload[field] = 7
                result = validate_payload(ArtifactKind.PROJECT_ENRICHMENT, payload)
                self.assertFalse(result.valid)
                self.assertIn(
                    f"/{field}",
                    {issue.instance_path for issue in result.issues},
                )
            with self.subTest(field=field, case="timezone missing"):
                payload = valid_project_enrichment()
                payload[field] = "2026-07-11T00:00:00"
                result = validate_payload(ArtifactKind.PROJECT_ENRICHMENT, payload)
                self.assertFalse(result.valid)
                self.assertIn(
                    f"/{field}",
                    {issue.instance_path for issue in result.issues},
                )

        payload = valid_project_enrichment()
        payload["analyzedAt"] = "2026-07-11T00:00:00"
        result = validate_payload(ArtifactKind.PROJECT_ENRICHMENT, payload)
        self.assertFalse(result.valid)
        self.assertIn("/analyzedAt", {issue.instance_path for issue in result.issues})

    def test_legacy_project_enrichment_remains_explicitly_non_current_shape(self) -> None:
        payload = valid_project_enrichment()
        payload["schemaVersion"] = 0
        payload.pop("sourcePushedAt")
        payload.pop("sourceAnalysisAt")

        self.assertTrue(
            validate_payload(ArtifactKind.PROJECT_ENRICHMENT, payload).valid
        )

    def test_rejects_repository_identity_mismatch(self) -> None:
        result = validate_payload(
            ArtifactKind.PROJECT_ENRICHMENT,
            valid_project_enrichment(),
            expected_repository="another/tool",
        )

        self.assertFalse(result.valid)
        self.assertIn("/repository", {issue.instance_path for issue in result.issues})

    def test_malformed_http_url_is_rejected_without_raising(self) -> None:
        for url in ("http://[", "https://example.com:bad/path", "https://example.com\\evil"):
            with self.subTest(url=url):
                payload = valid_project_enrichment()
                payload["sourceUrl"] = url
                result = validate_payload(ArtifactKind.PROJECT_ENRICHMENT, payload)
                self.assertFalse(result.valid)
                self.assertIn(
                    "/sourceUrl",
                    {issue.instance_path for issue in result.issues},
                )

    def test_rejects_out_of_range_signal_score(self) -> None:
        signal = {
            "id": "signal_test",
            "kind": "official",
            "title": "Test signal",
            "summaryZh": "测试动态。",
            "url": "https://example.com/news",
            "source": "Official News",
            "sourceUrl": "https://example.com/feed.xml",
            "publishedAt": "2026-07-11T00:00:00Z",
            "score": 1.01,
            "evidence": ["official_feed"],
            "sources": ["Official News"],
        }
        payload = {
            "schemaVersion": 1,
            "capturedAt": "2026-07-11T00:00:00Z",
            "windowHours": 48,
            "signalCount": 1,
            "healthySourceCount": 1,
            "failedSourceCount": 0,
            "sourceStatus": [
                {
                    "id": "official",
                    "name": "Official News",
                    "url": "https://example.com/feed.xml",
                    "state": "healthy",
                    "itemCount": 1,
                    "latestItemAt": "2026-07-11T00:00:00Z",
                    "error": None,
                }
            ],
            "topSignals": [signal],
            "signals": [signal],
        }

        result = validate_payload(ArtifactKind.TECHNICAL_SIGNALS, payload)

        self.assertFalse(result.valid)
        self.assertTrue(
            any(issue.instance_path.endswith("/score") for issue in result.issues)
        )

    def test_rejects_catalog_nested_field_type(self) -> None:
        catalog = valid_catalog()
        catalog["projects"][0]["capabilities"] = "not-an-array"  # type: ignore[index]

        result = validate_payload(ArtifactKind.CATALOG, catalog)

        self.assertFalse(result.valid)
        self.assertIn(
            "/projects/0/capabilities",
            {issue.instance_path for issue in result.issues},
        )

    def test_accepts_explicit_legacy_static_evidence_but_not_unversioned_data(self) -> None:
        legacy = {
            "schemaVersion": 0,
            "repository": "demo/tool",
            "source": "https://github.com/demo/tool",
            "scanned_files": 0,
            "language_files": {},
            "indicators": {
                "readme": False,
                "license": False,
                "tests": False,
                "ci": False,
                "docker": False,
                "dependency_lock": False,
                "package_manifest": False,
                "examples": False,
                "docs": False,
                "environment_example": False,
            },
            "counts": {"test_files": 0, "todo_markers": 0},
            "license_hint": None,
            "confidence": 0,
            "warnings": ["legacy evidence has no trustworthy analysis time"],
        }

        self.assertTrue(validate_payload(ArtifactKind.STATIC_EVIDENCE, legacy).valid)
        unversioned = deepcopy(legacy)
        del unversioned["schemaVersion"]
        self.assertFalse(validate_payload(ArtifactKind.STATIC_EVIDENCE, unversioned).valid)
        current_without_time = deepcopy(legacy)
        current_without_time["schemaVersion"] = 1
        self.assertFalse(
            validate_payload(ArtifactKind.STATIC_EVIDENCE, current_without_time).valid
        )
        committed_local = validate_payload(
            ArtifactKind.STATIC_EVIDENCE,
            {**legacy, "repository": "local"},
            source_path=Path("data/analysis/local.json"),
        )
        self.assertFalse(committed_local.valid)
        legacy_with_time = {**legacy, "analyzed_at": "2026-07-11T00:00:00Z"}
        self.assertFalse(
            validate_payload(ArtifactKind.STATIC_EVIDENCE, legacy_with_time).valid
        )

    def test_latest_snapshot_requires_complete_query_health_group(self) -> None:
        snapshot_path = REPOSITORY_ROOT / "data/snapshots/latest.json"
        snapshot = strict_json_loads(snapshot_path.read_text(encoding="utf-8"))
        legacy_shape = {
            key: value
            for key, value in snapshot.items()
            if key
            not in {
                "query_status",
                "successful_query_count",
                "failed_query_count",
            }
        }
        latest_without_health = validate_payload(
            ArtifactKind.GITHUB_SNAPSHOT,
            legacy_shape,
            source_path=Path("data/snapshots/latest.json"),
        )
        self.assertFalse(latest_without_health.valid)
        self.assertTrue(
            validate_payload(
                ArtifactKind.GITHUB_SNAPSHOT,
                legacy_shape,
                source_path=Path("data/snapshots/history/legacy.json"),
            ).valid
        )
        for field in ("query_status", "successful_query_count", "failed_query_count"):
            with self.subTest(field=field):
                candidate = deepcopy(snapshot)
                candidate.pop(field)
                latest = validate_payload(
                    ArtifactKind.GITHUB_SNAPSHOT,
                    candidate,
                    source_path=Path("data/snapshots/latest.json"),
                )
                self.assertFalse(latest.valid)

    def test_signal_enrichment_times_are_all_or_none(self) -> None:
        base = {
            "schemaVersion": 1,
            "generatedAt": "2026-07-11T00:00:00Z",
            "model": "local-codex",
            "items": {
                "https://example.com/news": {
                    "titleZh": "动态",
                    "takeawayZh": "摘要",
                    "whyItMattersZh": "影响",
                    "categoryZh": "AI",
                }
            },
        }
        self.assertTrue(validate_payload(ArtifactKind.SIGNAL_ENRICHMENT, base).valid)
        for field in ("analyzedAt", "sourcePublishedAt"):
            with self.subTest(field=field):
                candidate = deepcopy(base)
                candidate["items"]["https://example.com/news"][field] = (  # type: ignore[index]
                    "2026-07-11T00:00:00Z"
                )
                self.assertFalse(
                    validate_payload(ArtifactKind.SIGNAL_ENRICHMENT, candidate).valid
                )

    def test_strict_json_parser_rejects_duplicate_keys_and_non_finite_numbers(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate JSON object key"):
            strict_json_loads('{"schemaVersion": 1, "schemaVersion": 1}')
        with self.assertRaisesRegex(ValueError, "non-finite JSON number"):
            strict_json_loads('{"score": NaN}')

    def test_all_committed_artifacts_match_their_contracts(self) -> None:
        paths = sorted(
            path
            for path in (REPOSITORY_ROOT / "data").rglob("*.json")
            if infer_artifact_kind(path) is not None
        )

        self.assertGreaterEqual(len(paths), 7)
        for path in paths:
            with self.subTest(path=path):
                self.assertIsInstance(load_validated_json(path), dict)

    def test_committed_project_enrichments_bind_real_catalog_and_analysis_versions(self) -> None:
        catalog = load_validated_json(
            REPOSITORY_ROOT / "data/catalog/latest.json",
            ArtifactKind.CATALOG,
        )
        projects = {
            project["repo"]: project
            for project in catalog["projects"]
            if isinstance(project, dict) and isinstance(project.get("repo"), str)
        }
        bound_count = 0
        legacy_count = 0
        for path in sorted((REPOSITORY_ROOT / "data/enrichment").glob("*.json")):
            with self.subTest(path=path):
                enrichment = load_validated_json(
                    path,
                    ArtifactKind.PROJECT_ENRICHMENT,
                )
                repository = enrichment["repository"]
                analysis = load_validated_json(
                    REPOSITORY_ROOT / "data/analysis" / path.name,
                    ArtifactKind.STATIC_EVIDENCE,
                    expected_repository=repository,
                )
                if enrichment["schemaVersion"] == 1:
                    bound_count += 1
                    self.assertEqual(
                        enrichment["sourcePushedAt"],
                        projects[repository]["sourcePushedAt"],
                    )
                    self.assertEqual(
                        enrichment["sourceAnalysisAt"],
                        analysis["analyzed_at"],
                    )
                    enrichment_time = datetime.fromisoformat(
                        enrichment["analyzedAt"].replace("Z", "+00:00")
                    )
                    source_analysis_time = datetime.fromisoformat(
                        enrichment["sourceAnalysisAt"].replace("Z", "+00:00")
                    )
                    self.assertGreaterEqual(enrichment_time, source_analysis_time)
                else:
                    legacy_count += 1
                    if analysis["schemaVersion"] == 0:
                        self.assertNotIn("analyzed_at", analysis)
                    else:
                        enrichment_time = datetime.fromisoformat(
                            enrichment["analyzedAt"].replace("Z", "+00:00")
                        )
                        source_analysis_time = datetime.fromisoformat(
                            analysis["analyzed_at"].replace("Z", "+00:00")
                        )
                        self.assertLess(enrichment_time, source_analysis_time)

        self.assertEqual(bound_count, 4)
        self.assertEqual(legacy_count, 3)

    def test_audit_reports_schema_failure_with_json_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory) / "data"
            shutil.copytree(REPOSITORY_ROOT / "data", data_dir)
            catalog_path = data_dir / "catalog" / "latest.json"
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            catalog["projects"][0]["capabilities"] = "not-an-array"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

            result = audit_data(data_dir)

        schema_issues = [
            item for item in result["issues"] if item["code"] == "schema_validation_failed"
        ]
        self.assertEqual(result["status"], "failed")
        self.assertTrue(schema_issues)
        self.assertTrue(
            any("/projects/0/capabilities" in item["detail"] for item in schema_issues)
        )

    def test_audit_short_circuits_schema_invalid_core_containers(self) -> None:
        cases = (
            ("catalog/latest.json", "projects", "not-an-array"),
            ("signals/latest.json", "signals", "not-an-array"),
            ("queues/codex.json", "items", "not-an-array"),
        )
        for relative, field, value in cases:
            with self.subTest(path=relative, field=field):
                with tempfile.TemporaryDirectory() as directory:
                    data_dir = Path(directory) / "data"
                    shutil.copytree(REPOSITORY_ROOT / "data", data_dir)
                    path = data_dir / relative
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    payload[field] = value
                    path.write_text(json.dumps(payload), encoding="utf-8")

                    result = audit_data(data_dir)

                self.assertEqual(result["status"], "failed")
                self.assertIn(
                    "schema_validation_failed",
                    {item["code"] for item in result["issues"]},
                )

    def test_invalid_batch_is_rejected_before_replacing_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog_path = Path(directory) / "data" / "catalog" / "latest.json"
            catalog_path.parent.mkdir(parents=True)
            catalog_path.write_text('{"previous": true}\n', encoding="utf-8")
            before = catalog_path.read_bytes()
            invalid = valid_catalog()
            invalid["projects"][0]["capabilities"] = "not-an-array"  # type: ignore[index]

            with self.assertRaises(ArtifactValidationError):
                _write_json_batch([(catalog_path, invalid)])

            self.assertEqual(catalog_path.read_bytes(), before)

    def test_atomic_writer_rejects_cross_kind_target_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "data/signals/latest.json"
            target.parent.mkdir(parents=True)
            target.write_text('{"previous": true}\n', encoding="utf-8")
            before = target.read_bytes()

            with self.assertRaisesRegex(ValueError, "reserved for technical-signals"):
                atomic_write_validated_json(
                    target,
                    ArtifactKind.CATALOG,
                    valid_catalog(),
                )

            self.assertEqual(target.read_bytes(), before)

    def test_atomic_writer_allows_flat_staging_and_candidate_generation_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            flat_target = root / "data/catalog/latest.json"
            candidate_target = (
                root
                / "data/generations/.candidates/generation-001/catalog/latest.json"
            )

            atomic_write_validated_json(
                flat_target,
                ArtifactKind.CATALOG,
                valid_catalog(),
            )
            atomic_write_validated_json(
                candidate_target,
                ArtifactKind.CATALOG,
                valid_catalog(),
            )

            self.assertEqual(load_validated_json(flat_target), valid_catalog())
            self.assertEqual(load_validated_json(candidate_target), valid_catalog())

    def test_atomic_writer_rejects_final_generation_without_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = (
                Path(directory)
                / "data/generations/generation-001/catalog/latest.json"
            )
            target.parent.mkdir(parents=True)
            target.write_text('{"retained": true}\n', encoding="utf-8")
            before = target.read_bytes()

            with self.assertRaisesRegex(
                ValueError,
                "immutable published generation",
            ):
                atomic_write_validated_json(
                    target,
                    ArtifactKind.CATALOG,
                    valid_catalog(),
                )

            self.assertEqual(target.read_bytes(), before)

    def test_atomic_writer_final_generation_guard_is_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = (
                Path(directory)
                / "DATA/GENERATIONS/GENERATION-001/CATALOG/latest.json"
            )

            with self.assertRaisesRegex(
                ValueError,
                "immutable published generation",
            ):
                atomic_write_validated_json(
                    target,
                    ArtifactKind.CATALOG,
                    valid_catalog(),
                )

            self.assertFalse(target.exists())

    def test_atomic_writer_rejects_alias_resolving_into_final_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            alias_target = root / "data/alias/latest.json"
            final_target = (
                root / "data/generations/generation-001/catalog/latest.json"
            ).resolve()
            original_resolve = Path.resolve

            def resolve_path(path: Path, strict: bool = False) -> Path:
                if path == alias_target:
                    return final_target
                return original_resolve(path, strict=strict)

            with patch.object(Path, "resolve", new=resolve_path):
                with self.assertRaisesRegex(
                    ValueError,
                    "immutable published generation",
                ):
                    atomic_write_validated_json(
                        alias_target,
                        ArtifactKind.CATALOG,
                        valid_catalog(),
                    )

            self.assertFalse(alias_target.exists())

    def test_atomic_writer_publishes_valid_repository_identity_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "data/enrichment/demo--tool.json"
            payload = valid_project_enrichment()

            atomic_write_validated_json(
                target,
                ArtifactKind.PROJECT_ENRICHMENT,
                payload,
                expected_repository="demo/tool",
            )

            self.assertEqual(load_validated_json(target), payload)
            self.assertEqual(list(target.parent.glob("*.tmp")), [])

    def test_atomic_writer_rejects_older_completed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "data/catalog/latest.json"
            target.parent.mkdir(parents=True)
            existing = valid_catalog()
            existing["capturedAt"] = "2026-07-11T12:00:00Z"
            target.write_text(json.dumps(existing), encoding="utf-8")
            before = target.read_bytes()
            older = valid_catalog()
            older["capturedAt"] = "2026-07-11T11:00:00Z"

            with self.assertRaisesRegex(ValueError, "refusing to replace newer"):
                atomic_write_validated_json(target, ArtifactKind.CATALOG, older)

            self.assertEqual(target.read_bytes(), before)

    def test_atomic_writer_rejects_safe_name_repository_collision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "data/enrichment/demo--tool-a.json"
            target.parent.mkdir(parents=True)
            existing = valid_project_enrichment()
            existing["repository"] = "demo/tool_a"
            target.write_text(json.dumps(existing), encoding="utf-8")
            candidate = valid_project_enrichment()
            candidate["repository"] = "demo/tool-a"

            with self.assertRaisesRegex(ValueError, "already belongs to repository"):
                atomic_write_validated_json(
                    target,
                    ArtifactKind.PROJECT_ENRICHMENT,
                    candidate,
                )


if __name__ == "__main__":
    unittest.main()
