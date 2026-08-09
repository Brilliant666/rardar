import copy
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "contracts" / "legacy-project-identity-dispositions.schema.json"
POLICY_PATH = ROOT / "contracts" / "legacy-project-identity-dispositions.json"


class LegacyProjectIdentityPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        cls.policy = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(cls.schema)
        cls.validator = Draft202012Validator(cls.schema)

    def test_formal_policy_is_exact_and_schema_valid(self):
        self.validator.validate(self.policy)
        self.assertEqual(self.policy, {
            "schemaVersion": 1,
            "policyVersion": "2026-07-18.1",
            "entries": [{
                "projectSlug": "officecli",
                "disposition": "quarantine",
                "reasonCode": "no_verified_repository_in_current_or_retained_catalogs",
                "sourceTables": ["feedback"],
            }],
        })

    def test_schema_rejects_wildcard_and_wrong_source_scope(self):
        wildcard = copy.deepcopy(self.policy)
        wildcard["entries"][0]["projectSlug"] = "office*"
        with self.assertRaises(ValidationError):
            self.validator.validate(wildcard)

        wrong_scope = copy.deepcopy(self.policy)
        wrong_scope["entries"][0]["sourceTables"] = ["feedback_v2"]
        with self.assertRaises(ValidationError):
            self.validator.validate(wrong_scope)

    def test_schema_rejects_repository_project_id_and_device_fields(self):
        for field in ("repository", "projectId", "deviceId", "device_id"):
            candidate = copy.deepcopy(self.policy)
            candidate["entries"][0][field] = "must-not-be-stored"
            with self.subTest(field=field), self.assertRaises(ValidationError):
                self.validator.validate(candidate)

        def all_keys(value):
            if isinstance(value, dict):
                return set(value).union(*(all_keys(item) for item in value.values()))
            if isinstance(value, list):
                return set().union(*(all_keys(item) for item in value))
            return set()

        keys = all_keys(self.policy)
        self.assertFalse({"deviceId", "device_id", "repository", "projectId"} & keys)

    def test_schema_rejects_duplicate_source_tables(self):
        duplicate = copy.deepcopy(self.policy)
        duplicate["entries"][0]["sourceTables"] = ["feedback", "feedback"]
        with self.assertRaises(ValidationError):
            self.validator.validate(duplicate)


if __name__ == "__main__":
    unittest.main()
