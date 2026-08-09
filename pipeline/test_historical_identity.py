from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from jsonschema import Draft202012Validator, FormatChecker

from pipeline.generations import (
    CandidateGenerationError,
    ResolvedGeneration,
    create_candidate_generation,
    fail_candidate_generation,
    publish_candidate_generation,
    rollback_to_generation,
    verify_retained_generation,
)
from pipeline.historical_identity import (
    HistoricalIdentityBundleError,
    _mapping_for_project,
    _validate_cross_generation_mappings,
    build_historical_identity_bundle,
)
from pipeline.project_identity import identity_for_repository
from pipeline.test_generations import _ready_refresh_candidate


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _copy_published_data(target: Path) -> None:
    target.mkdir()
    shutil.copy2(REPOSITORY_ROOT / "data" / "current.json", target / "current.json")
    shutil.copytree(REPOSITORY_ROOT / "data" / "generations", target / "generations")


def _read(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def _write(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _synthetic_mapping(
    repository: str,
    slug: str,
    generation_id: str,
) -> dict[str, object]:
    identity = identity_for_repository(repository)
    return {
        "generationId": generation_id,
        "projectId": identity.project_id,
        "canonicalRepository": identity.canonical_repository,
        "projectSlug": slug,
    }


class HistoricalIdentityBundleTests(unittest.TestCase):
    def test_builder_preserves_provenance_for_an_empty_current_catalog(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            root = data_dir / "generations" / "empty-current"
            (root / "catalog").mkdir(parents=True)
            catalog_path = root / "catalog" / "latest.json"
            _write(catalog_path, {"schemaVersion": 3, "projects": []})
            catalog_digest = hashlib.sha256(catalog_path.read_bytes()).hexdigest()
            manifest = {
                "generationId": "empty-current",
                "createdAt": "2026-07-18T00:00:00Z",
                "hashes": {"catalog/latest.json": catalog_digest},
                "audit": {"status": "healthy"},
            }
            manifest_path = root / "manifest.json"
            _write(manifest_path, manifest)
            manifest_digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            published_at = "2026-07-18T00:00:01Z"
            current = ResolvedGeneration(
                data_dir=data_dir.resolve(),
                generation_id="empty-current",
                root=root.resolve(),
                pointer={
                    "generationId": "empty-current",
                    "publishedAt": published_at,
                    "manifestSha256": manifest_digest,
                },
                manifest=manifest,
                legacy=False,
            )

            with patch("pipeline.historical_identity.resolve_current_generation", return_value=current):
                bundle = build_historical_identity_bundle(data_dir)

            self.assertEqual(bundle["generationCount"], 1)
            self.assertEqual(bundle["mappingCount"], 0)
            self.assertEqual(bundle["mappings"], [])
            self.assertEqual(
                bundle["generations"],
                [{
                    "generationId": "empty-current",
                    "generationCreatedAt": "2026-07-18T00:00:00Z",
                    "publishedAt": published_at,
                    "manifestSha256": manifest_digest,
                    "catalogSchemaVersion": 3,
                    "active": True,
                }],
            )

    def test_builds_current_and_retained_v1_v2_v3_without_flat_or_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _copy_published_data(data_dir)
            candidate, _ = _ready_refresh_candidate(data_dir, "historical-v3")
            publish_candidate_generation(candidate)

            broken_candidate = data_dir / "generations" / ".candidates" / "ignored"
            broken_candidate.mkdir(parents=True)
            (broken_candidate / "manifest.json").write_text("not json", encoding="utf-8")
            flat_catalog = data_dir / "catalog" / "latest.json"
            flat_catalog.parent.mkdir(parents=True, exist_ok=True)
            flat_catalog.write_text("not json", encoding="utf-8")

            bundle = build_historical_identity_bundle(data_dir)
            schema = _read(REPOSITORY_ROOT / "contracts/historical-identity-bundle.schema.json")
            Draft202012Validator(schema, format_checker=FormatChecker()).validate(bundle)

            self.assertEqual(bundle["activeGenerationId"], "historical-v3")
            pointer = _read(data_dir / "current.json")
            self.assertEqual(bundle["activePublishedAt"], pointer["publishedAt"])
            self.assertEqual(bundle["generationCount"], 3)
            self.assertEqual(bundle["generationCount"], len(bundle["generations"]))
            self.assertEqual(sum(item["active"] for item in bundle["generations"]), 1)
            self.assertEqual(
                {mapping["catalogSchemaVersion"] for mapping in bundle["mappings"]},
                {1, 2, 3},
            )
            self.assertEqual(bundle["mappingCount"], len(bundle["mappings"]))
            for mapping in bundle["mappings"]:
                if mapping["active"]:
                    self.assertEqual(mapping["publishedAt"], pointer["publishedAt"])
                else:
                    self.assertIsNone(mapping["publishedAt"])

    def test_public_retained_verifier_is_the_rollback_validation_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _copy_published_data(data_dir)
            pointer = _read(data_dir / "current.json")
            retained = next(
                path.name
                for path in sorted((data_dir / "generations").iterdir())
                if not path.name.startswith(".") and path.name != pointer["generationId"]
            )
            verified = verify_retained_generation(data_dir, retained)
            self.assertEqual(
                verified.manifest_sha256,
                hashlib.sha256((verified.root / "manifest.json").read_bytes()).hexdigest(),
            )
            with patch(
                "pipeline.generations.verify_retained_generation",
                wraps=verify_retained_generation,
            ) as verifier:
                result = rollback_to_generation(data_dir, retained)
            self.assertEqual(result.current.generation_id, retained)
            verifier.assert_called_once()

    def test_visible_failed_damaged_and_schema_invalid_finals_fail_closed(self) -> None:
        cases = ("failed", "damaged", "schema")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                data_dir = Path(temporary) / "data"
                _copy_published_data(data_dir)
                if case == "failed":
                    candidate = create_candidate_generation(
                        data_dir,
                        "derive",
                        generation_id="failed-final",
                        overlay_flat_staging=False,
                    )
                    fail_candidate_generation(candidate, "build", "expected test failure")
                    shutil.move(
                        str(candidate.path),
                        str(data_dir / "generations" / "failed-final"),
                    )
                elif case == "damaged":
                    final = data_dir / "generations" / "damaged-final"
                    final.mkdir()
                    (final / "manifest.json").write_text("not json", encoding="utf-8")
                else:
                    retained = next(
                        path
                        for path in sorted((data_dir / "generations").iterdir())
                        if not path.name.startswith(".")
                        and path.name != _read(data_dir / "current.json")["generationId"]
                    )
                    catalog_path = retained / "catalog" / "latest.json"
                    catalog = _read(catalog_path)
                    catalog["projects"] = "not an array"
                    _write(catalog_path, catalog)
                    manifest_path = retained / "manifest.json"
                    manifest = _read(manifest_path)
                    manifest["hashes"]["catalog/latest.json"] = hashlib.sha256(
                        catalog_path.read_bytes()
                    ).hexdigest()
                    _write(manifest_path, manifest)
                with self.assertRaises((CandidateGenerationError, HistoricalIdentityBundleError)):
                    build_historical_identity_bundle(data_dir)

    def test_visible_symlink_final_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary) / "data"
            _copy_published_data(data_dir)
            source = next(
                path for path in (data_dir / "generations").iterdir()
                if not path.name.startswith(".")
            )
            link = data_dir / "generations" / "linked-final"
            try:
                os.symlink(source, link, target_is_directory=True)
            except OSError as error:
                self.skipTest(f"directory symlinks are unavailable: {error}")
            with self.assertRaises(CandidateGenerationError) as raised:
                build_historical_identity_bundle(data_dir)
            self.assertEqual(raised.exception.code, "unsafe_symlink")

    def test_v3_recomputes_carried_identity(self) -> None:
        identity = identity_for_repository("owner/repository")
        project = {
            "repo": "owner/repository",
            "slug": "owner--repository",
            "projectIdVersion": 1,
            "projectId": identity.project_id[:-1] + ("0" if identity.project_id[-1] != "0" else "1"),
        }
        with self.assertRaises(HistoricalIdentityBundleError) as raised:
            _mapping_for_project(
                project,
                catalog_schema_version=3,
                generation_id="generation-v3",
                generation_created_at="2026-07-18T00:00:00Z",
                manifest_sha256="a" * 64,
                active=False,
                active_published_at="2026-07-18T00:00:01Z",
            )
        self.assertEqual(raised.exception.code, "project_id_mismatch")

    def test_cross_generation_collisions_fail_but_same_project_slug_rename_is_allowed(self) -> None:
        old = _synthetic_mapping("owner/project", "old-slug", "generation-1")
        renamed = _synthetic_mapping("owner/project", "new-slug", "generation-2")
        _validate_cross_generation_mappings([old, renamed])

        rebind = _synthetic_mapping("other/project", "old-slug", "generation-3")
        with self.assertRaises(HistoricalIdentityBundleError) as raised:
            _validate_cross_generation_mappings([old, rebind])
        self.assertEqual(raised.exception.code, "historical_project_slug_rebind")

        id_collision = dict(_synthetic_mapping("different/project", "different", "generation-4"))
        id_collision["projectId"] = old["projectId"]
        with self.assertRaises(HistoricalIdentityBundleError) as raised:
            _validate_cross_generation_mappings([old, id_collision])
        self.assertEqual(raised.exception.code, "historical_project_id_collision")

        repository_collision = dict(
            _synthetic_mapping("another/project", "another", "generation-5")
        )
        repository_collision["canonicalRepository"] = old["canonicalRepository"]
        with self.assertRaises(HistoricalIdentityBundleError) as raised:
            _validate_cross_generation_mappings([old, repository_collision])
        self.assertEqual(raised.exception.code, "historical_repository_collision")


if __name__ == "__main__":
    unittest.main()
