"""Canonical, versioned project identity for GitHub repositories.

Identity v1 deliberately derives from the case-insensitive ``owner/repo``
name rather than the legacy lossy slug.  The readable prefix is only a label;
the truncated SHA-256 digest is the collision-resistant identity component.
Callers that build a collection must still reject any observed collision with
``ensure_unique_project_identities`` rather than choosing one repository.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Sequence


PROJECT_ID_VERSION = 1
PROJECT_ID_ALGORITHM = "rardar-project-id-v1"
PROJECT_ID_PREFIX_MAX_LENGTH = 64
PROJECT_ID_DIGEST_HEX_LENGTH = 20
# The shortest valid repository is ``a/b``; its readable prefix is ``a-b``.
PROJECT_ID_MIN_LENGTH = 3 + 2 + PROJECT_ID_DIGEST_HEX_LENGTH
PROJECT_ID_MAX_LENGTH = (
    PROJECT_ID_PREFIX_MAX_LENGTH + 2 + PROJECT_ID_DIGEST_HEX_LENGTH
)

# GitHub.com account names are at most 39 ASCII characters and cannot start,
# end, or repeat a hyphen. Repository names are at most 100 characters and use
# GitHub's documented filename-safe alphabet. A repository segment of only
# ``.`` or ``..`` is rejected explicitly because it carries path semantics.
REPOSITORY_PATTERN_TEXT = (
    r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}/"
    r"(?!\.{1,2}$)[A-Za-z0-9._-]{1,100}$"
)
PROJECT_ID_PATTERN_TEXT = r"^[a-z0-9]+(?:-[a-z0-9]+)*--[0-9a-f]{20}$"

REPOSITORY_PATTERN = re.compile(REPOSITORY_PATTERN_TEXT)
PROJECT_ID_PATTERN = re.compile(PROJECT_ID_PATTERN_TEXT)
_PREFIX_UNSAFE_PATTERN = re.compile(r"[^a-z0-9]+")
_LEGACY_UNSAFE_PATTERN = re.compile(r"[^a-z0-9-]+")


class ProjectIdentityErrorCode(str, Enum):
    INVALID_REPOSITORY_TYPE = "invalid_repository_type"
    INVALID_REPOSITORY_FORMAT = "invalid_repository_format"
    INVALID_PROJECT_ID = "invalid_project_id"
    UNSUPPORTED_PROJECT_ID_VERSION = "unsupported_project_id_version"
    PROJECT_ID_MISMATCH = "project_id_mismatch"
    DUPLICATE_NORMALIZED_REPOSITORY = "duplicate_normalized_repository"
    PROJECT_ID_COLLISION = "project_id_collision"


class ProjectIdentityError(ValueError):
    """A stable identity-contract failure suitable for structured reporting."""

    def __init__(
        self,
        code: ProjectIdentityErrorCode | str,
        message: str,
        *,
        repository: object | None = None,
        project_id: object | None = None,
    ) -> None:
        self.code = (
            code.value if isinstance(code, ProjectIdentityErrorCode) else str(code)
        )
        self.repository = repository
        self.project_id = project_id
        super().__init__(message)


@dataclass(frozen=True)
class ProjectIdentity:
    project_id_version: int
    canonical_repository: str
    human_prefix: str
    digest: str
    project_id: str

    def as_dict(self) -> dict[str, object]:
        return {
            "projectIdVersion": self.project_id_version,
            "canonicalRepository": self.canonical_repository,
            "humanPrefix": self.human_prefix,
            "digest": self.digest,
            "projectId": self.project_id,
        }


def canonicalize_repository(repository: object) -> str:
    """Validate an exact GitHub ``owner/repo`` and return ASCII lowercase.

    This function is intentionally strict: it does not trim whitespace, parse
    URLs, remove a ``.git`` suffix, or otherwise guess what the caller meant.
    The original spelling remains available to callers for display/source
    binding while this normalized form is used only for identity.
    """

    if not isinstance(repository, str):
        raise ProjectIdentityError(
            ProjectIdentityErrorCode.INVALID_REPOSITORY_TYPE,
            "repository must be a string in exact GitHub owner/repo form",
            repository=repository,
        )
    if not REPOSITORY_PATTERN.fullmatch(repository):
        raise ProjectIdentityError(
            ProjectIdentityErrorCode.INVALID_REPOSITORY_FORMAT,
            (
                "repository must be an exact GitHub owner/repo: owner is 1-39 "
                "ASCII letters, digits, or single internal hyphens; repository "
                "is 1-100 ASCII letters, digits, dots, underscores, or hyphens"
            ),
            repository=repository,
        )
    return repository.lower()


def _human_prefix(canonical_repository: str) -> str:
    prefix = _PREFIX_UNSAFE_PATTERN.sub("-", canonical_repository).strip("-")
    prefix = prefix[:PROJECT_ID_PREFIX_MAX_LENGTH].rstrip("-")
    # A valid owner always begins with an ASCII alphanumeric character, so the
    # prefix cannot become empty even when a repository is punctuation-only.
    if not prefix:  # Defensive assertion at the public contract boundary.
        raise ProjectIdentityError(
            ProjectIdentityErrorCode.INVALID_REPOSITORY_FORMAT,
            "repository does not produce a safe human-readable prefix",
            repository=canonical_repository,
        )
    return prefix


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def identity_for_repository(repository: object) -> ProjectIdentity:
    canonical = canonicalize_repository(repository)
    prefix = _human_prefix(canonical)
    digest = _sha256_hex(canonical)[:PROJECT_ID_DIGEST_HEX_LENGTH]
    project_id = f"{prefix}--{digest}"
    if (
        len(project_id) > PROJECT_ID_MAX_LENGTH
        or not PROJECT_ID_PATTERN.fullmatch(project_id)
    ):
        raise AssertionError("identity v1 generated an invalid project ID")
    return ProjectIdentity(
        project_id_version=PROJECT_ID_VERSION,
        canonical_repository=canonical,
        human_prefix=prefix,
        digest=digest,
        project_id=project_id,
    )


def project_id_for_repository(repository: object) -> str:
    return identity_for_repository(repository).project_id


def is_project_id(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and PROJECT_ID_MIN_LENGTH <= len(value) <= PROJECT_ID_MAX_LENGTH
        and PROJECT_ID_PATTERN.fullmatch(value)
    )


def validate_project_identity(
    repository: object,
    project_id: object,
    project_id_version: object,
) -> ProjectIdentity:
    """Recompute and verify carried identity fields without trusting either."""

    identity = identity_for_repository(repository)
    if (
        not isinstance(project_id_version, int)
        or isinstance(project_id_version, bool)
        or project_id_version != PROJECT_ID_VERSION
    ):
        raise ProjectIdentityError(
            ProjectIdentityErrorCode.UNSUPPORTED_PROJECT_ID_VERSION,
            f"projectIdVersion must be {PROJECT_ID_VERSION}",
            repository=repository,
            project_id=project_id,
        )
    if not is_project_id(project_id):
        raise ProjectIdentityError(
            ProjectIdentityErrorCode.INVALID_PROJECT_ID,
            "projectId does not match the identity v1 safe format",
            repository=repository,
            project_id=project_id,
        )
    if project_id != identity.project_id:
        raise ProjectIdentityError(
            ProjectIdentityErrorCode.PROJECT_ID_MISMATCH,
            "projectId does not match the identity recomputed from repository",
            repository=repository,
            project_id=project_id,
        )
    return identity


def ensure_unique_project_identities(
    repositories: Iterable[object],
) -> dict[str, ProjectIdentity]:
    """Return identities by canonical repository or fail on duplicates/collision."""

    by_repository: dict[str, ProjectIdentity] = {}
    repository_by_project_id: dict[str, str] = {}
    for repository in repositories:
        identity = identity_for_repository(repository)
        if identity.canonical_repository in by_repository:
            raise ProjectIdentityError(
                ProjectIdentityErrorCode.DUPLICATE_NORMALIZED_REPOSITORY,
                (
                    "multiple repository rows normalize to "
                    f"{identity.canonical_repository!r}"
                ),
                repository=repository,
                project_id=identity.project_id,
            )
        existing_repository = repository_by_project_id.get(identity.project_id)
        if (
            existing_repository is not None
            and existing_repository != identity.canonical_repository
        ):
            raise ProjectIdentityError(
                ProjectIdentityErrorCode.PROJECT_ID_COLLISION,
                (
                    f"projectId {identity.project_id!r} belongs to both "
                    f"{existing_repository!r} and {identity.canonical_repository!r}"
                ),
                repository=repository,
                project_id=identity.project_id,
            )
        by_repository[identity.canonical_repository] = identity
        repository_by_project_id[identity.project_id] = identity.canonical_repository
    return by_repository


def legacy_slug_for_repository(repository: object) -> str:
    """Reproduce the pre-v1 lossy filename/slug for migration detection only."""

    canonical = canonicalize_repository(repository)
    return _LEGACY_UNSAFE_PATTERN.sub(
        "-", canonical.replace("/", "--")
    ).strip("-")


def _success_payload(repository: str) -> dict[str, object]:
    identity = identity_for_repository(repository)
    return {
        "schemaVersion": 1,
        "status": "ok",
        "algorithm": PROJECT_ID_ALGORITHM,
        "repository": repository,
        **identity.as_dict(),
    }


def _error_payload(error: ProjectIdentityError) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "status": "error",
        "algorithm": PROJECT_ID_ALGORITHM,
        "errorCode": error.code,
        "error": str(error),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute one canonical Rardar project identity as JSON"
    )
    parser.add_argument("--repository", required=True, help="exact GitHub owner/repo")
    arguments = parser.parse_args(argv)
    try:
        payload = _success_payload(arguments.repository)
    except ProjectIdentityError as error:
        print(json.dumps(_error_payload(error), ensure_ascii=False, allow_nan=False))
        return 2
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
