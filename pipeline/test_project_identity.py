from __future__ import annotations

import io
import json
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from pipeline.project_identity import (
    PROJECT_ID_ALGORITHM,
    PROJECT_ID_DIGEST_HEX_LENGTH,
    PROJECT_ID_MAX_LENGTH,
    PROJECT_ID_MIN_LENGTH,
    PROJECT_ID_PATTERN_TEXT,
    PROJECT_ID_PREFIX_MAX_LENGTH,
    PROJECT_ID_VERSION,
    REPOSITORY_PATTERN_TEXT,
    ProjectIdentityError,
    canonicalize_repository,
    ensure_unique_project_identities,
    identity_for_repository,
    is_project_id,
    legacy_slug_for_repository,
    main,
    project_id_for_repository,
    validate_project_identity,
)


ROOT = Path(__file__).resolve().parent.parent
VECTORS_PATH = ROOT / "contracts" / "project-identity-v1.vectors.json"


def load_vectors() -> dict[str, object]:
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


class ProjectIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.vectors = load_vectors()

    def test_vector_metadata_matches_implementation(self) -> None:
        self.assertEqual(self.vectors["schemaVersion"], 1)
        self.assertEqual(self.vectors["algorithm"], PROJECT_ID_ALGORITHM)
        self.assertEqual(self.vectors["projectIdVersion"], PROJECT_ID_VERSION)
        self.assertEqual(self.vectors["repositoryPattern"], REPOSITORY_PATTERN_TEXT)
        self.assertEqual(self.vectors["projectIdPattern"], PROJECT_ID_PATTERN_TEXT)
        self.assertEqual(
            self.vectors["prefixMaxLength"], PROJECT_ID_PREFIX_MAX_LENGTH
        )
        self.assertEqual(
            self.vectors["digestHexLength"], PROJECT_ID_DIGEST_HEX_LENGTH
        )
        self.assertEqual(self.vectors["projectIdMinLength"], PROJECT_ID_MIN_LENGTH)
        self.assertEqual(self.vectors["projectIdMaxLength"], PROJECT_ID_MAX_LENGTH)

    def test_all_valid_golden_vectors(self) -> None:
        for vector in self.vectors["valid"]:
            with self.subTest(vector=vector["name"]):
                identity = identity_for_repository(vector["repository"])
                self.assertEqual(
                    identity.canonical_repository, vector["canonicalRepository"]
                )
                self.assertEqual(identity.human_prefix, vector["humanPrefix"])
                self.assertEqual(identity.digest, vector["digest"])
                self.assertEqual(identity.project_id, vector["projectId"])
                self.assertEqual(
                    project_id_for_repository(vector["repository"]),
                    vector["projectId"],
                )
                self.assertEqual(
                    legacy_slug_for_repository(vector["repository"]),
                    vector["legacySlug"],
                )
                self.assertTrue(is_project_id(identity.project_id))

    def test_all_invalid_repository_vectors(self) -> None:
        for vector in self.vectors["invalid"]:
            with self.subTest(vector=vector["name"]):
                with self.assertRaises(ProjectIdentityError) as raised:
                    identity_for_repository(vector["repository"])
                self.assertEqual(raised.exception.code, vector["errorCode"])

    def test_carried_identity_validation_vectors(self) -> None:
        for vector in self.vectors["identityValidation"]:
            with self.subTest(vector=vector["name"]):
                if vector["valid"]:
                    identity = validate_project_identity(
                        vector["repository"],
                        vector["projectId"],
                        vector["projectIdVersion"],
                    )
                    self.assertEqual(identity.project_id, vector["projectId"])
                else:
                    with self.assertRaises(ProjectIdentityError) as raised:
                        validate_project_identity(
                            vector["repository"],
                            vector["projectId"],
                            vector["projectIdVersion"],
                        )
                    self.assertEqual(raised.exception.code, vector["errorCode"])

    def test_identity_is_deterministic_and_case_insensitive(self) -> None:
        expected = "owner-repo--65e817eec8cd71edae74"
        for repository in ("Owner/Repo", "owner/repo", "OWNER/REPO"):
            self.assertEqual(project_id_for_repository(repository), expected)
            self.assertEqual(canonicalize_repository(repository), "owner/repo")
        self.assertEqual(project_id_for_repository("owner/repo"), expected)

    def test_owner_or_repository_rename_creates_a_new_identity(self) -> None:
        original = project_id_for_repository("owner/repo")
        self.assertNotEqual(original, project_id_for_repository("new-owner/repo"))
        self.assertNotEqual(original, project_id_for_repository("owner/new-repo"))

    def test_known_legacy_slug_collisions_have_distinct_project_ids(self) -> None:
        for group in self.vectors["legacyCollisionGroups"]:
            with self.subTest(legacy_slug=group["legacySlug"]):
                repositories = group["repositories"]
                self.assertEqual(
                    {legacy_slug_for_repository(item) for item in repositories},
                    {group["legacySlug"]},
                )
                generated = [project_id_for_repository(item) for item in repositories]
                self.assertEqual(generated, group["projectIds"])
                self.assertEqual(len(generated), len(set(generated)))

    def test_project_id_is_filename_and_url_segment_safe(self) -> None:
        for vector in self.vectors["valid"]:
            project_id = project_id_for_repository(vector["repository"])
            self.assertLessEqual(len(project_id), PROJECT_ID_MAX_LENGTH)
            self.assertNotIn("/", project_id)
            self.assertNotIn("\\", project_id)
            self.assertNotIn("..", project_id)
            self.assertFalse(any(ord(character) < 32 for character in project_id))
            self.assertEqual(Path(f"{project_id}.json").name, f"{project_id}.json")

    def test_maximum_input_bounds_and_prefix_truncation(self) -> None:
        maximum = next(
            vector
            for vector in self.vectors["valid"]
            if vector["name"] == "maximum-owner-and-repository"
        )
        owner, repository = maximum["repository"].split("/")
        identity = identity_for_repository(maximum["repository"])
        self.assertEqual(len(owner), 39)
        self.assertEqual(len(repository), 100)
        self.assertEqual(len(identity.human_prefix), PROJECT_ID_PREFIX_MAX_LENGTH)
        self.assertEqual(len(identity.project_id), PROJECT_ID_MAX_LENGTH)

    def test_duplicate_normalized_repository_is_rejected(self) -> None:
        with self.assertRaises(ProjectIdentityError) as raised:
            ensure_unique_project_identities(["Owner/Repo", "owner/repo"])
        self.assertEqual(
            raised.exception.code, "duplicate_normalized_repository"
        )

    def test_observed_project_id_collision_is_rejected(self) -> None:
        # These repositories intentionally share their readable prefix. Forcing
        # the digest makes the normally-improbable collision path deterministic.
        with patch(
            "pipeline.project_identity._sha256_hex",
            return_value="0" * 64,
        ):
            with self.assertRaises(ProjectIdentityError) as raised:
                ensure_unique_project_identities(
                    ["owner/foo.bar", "owner/foo-bar"]
                )
        self.assertEqual(raised.exception.code, "project_id_collision")

    def test_well_formed_identity_for_another_repository_is_rejected(self) -> None:
        forged = project_id_for_repository("other/repo")
        self.assertTrue(is_project_id(forged))
        with self.assertRaises(ProjectIdentityError) as raised:
            validate_project_identity("owner/repo", forged, 1)
        self.assertEqual(raised.exception.code, "project_id_mismatch")

    def test_boolean_version_is_not_an_integer_version(self) -> None:
        with self.assertRaises(ProjectIdentityError) as raised:
            validate_project_identity(
                "owner/repo",
                project_id_for_repository("owner/repo"),
                True,
            )
        self.assertEqual(raised.exception.code, "unsupported_project_id_version")

    def test_cli_success_and_failure_are_json_on_stdout(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["--repository", "Owner/Repo"])
        self.assertEqual(exit_code, 0)
        success = json.loads(output.getvalue())
        self.assertEqual(success["status"], "ok")
        self.assertEqual(success["canonicalRepository"], "owner/repo")
        self.assertEqual(
            success["projectId"], "owner-repo--65e817eec8cd71edae74"
        )

        output = io.StringIO()
        with redirect_stdout(output):
            exit_code = main(["--repository", "owner/../repo"])
        self.assertEqual(exit_code, 2)
        failure = json.loads(output.getvalue())
        self.assertEqual(failure["status"], "error")
        self.assertEqual(failure["errorCode"], "invalid_repository_format")

    def test_module_cli_is_consumable_by_node_processes(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pipeline.project_identity",
                "--repository",
                "owner/repo",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stderr, "")
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["projectIdVersion"], 1)
        self.assertEqual(
            payload["projectId"], "owner-repo--65e817eec8cd71edae74"
        )


if __name__ == "__main__":
    unittest.main()
