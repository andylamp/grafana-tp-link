# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Exercise authenticated patch/minor decisions without making GitHub API calls."""

from __future__ import annotations

import runpy
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml

if TYPE_CHECKING:
    from collections.abc import Callable

ApiObject = dict[str, Any]
ROOT = Path(__file__).resolve().parents[1]
NAMESPACE = runpy.run_path(str(ROOT / ".github/scripts/dependabot_policy.py"))
evaluate = cast("Callable[[object, object], dict[str, bool | str]]", NAMESPACE["evaluate"])


def make_case(dependencies: list[ApiObject], descriptions: list[str]) -> tuple[ApiObject, list[ApiObject]]:
    """Create verified bot commits with one cumulative metadata trailer on the head.

    Returns
    -------
    tuple[ApiObject, list[ApiObject]]
        Pull-request metadata and the complete ordered commit array.
    """
    records: list[ApiObject] = [
        {
            "sha": f"{index:040x}",
            "author": {"login": "dependabot[bot]"},
            "committer": {"login": "web-flow"},
            "commit": {"message": description, "verification": {"verified": True, "reason": "valid"}},
        }
        for index, description in enumerate(descriptions, start=1)
    ]
    records[-1]["commit"]["message"] += (
        "\n\n---\n"
        + yaml.safe_dump({"updated-dependencies": dependencies})
        + "...\n\nSigned-off-by: dependabot[bot]\n"
    )
    pull = {
        "user": {"login": "dependabot[bot]"},
        "state": "open",
        "draft": False,
        "head": {"sha": records[-1]["sha"], "repo": {"full_name": "andylamp/grafana-tp-link"}},
        "base": {"ref": "master", "repo": {"full_name": "andylamp/grafana-tp-link"}},
        "commits": len(records),
    }
    return pull, records


@pytest.fixture
def mixed_case() -> tuple[ApiObject, list[ApiObject]]:
    """Mirror PR15's mixed group with cumulative metadata only on its second commit.

    Returns
    -------
    tuple[ApiObject, list[ApiObject]]
        Three declared Python patches and a Docker minor update with no declared update type.
    """
    dependencies = [
        {"dependency-name": name, "dependency-version": new, "update-type": "version-update:semver-patch"}
        for name, new in (("prek", "0.5.10"), ("ruff", "0.17.4"), ("ty", "0.0.98"))
    ]
    dependencies.append({"dependency-name": "prometheus/prometheus", "dependency-version": "v3.16.0"})
    return make_case(
        dependencies,
        [
            (
                "Updates `prek` from 0.5.7 to 0.5.10\n"
                "Updates `ruff` from 0.17.3 to 0.17.4\n"
                "Updates `ty` from 0.0.95 to 0.0.98"
            ),
            "Updates `prometheus/prometheus` from v3.15.1 to v3.16.0",
        ],
    )


def test_mixed_group_checks_every_commit_and_infers_docker_minor(
    mixed_case: tuple[ApiObject, list[ApiObject]],
) -> None:
    """Python patches plus a proven Docker minor upgrade may merge together."""
    pull, commits = mixed_case
    result = evaluate(pull, commits)
    assert result["eligible"] is True
    assert result["head_sha"] == pull["head"]["sha"]


@pytest.mark.parametrize(
    "description",
    [
        "Updates `example` from 1.0.0 to 1.0.1",
        "Bumps example from 1.0.0 to 1.0.1.",
        "Bumps [example](https://example.com/example) from 1.0.0 to 1.0.1.",
    ],
)
def test_single_patch_description_formats_are_supported(description: str) -> None:
    """Authenticated individual and grouped version descriptions use the same strict policy."""
    pull, commits = make_case([{"dependency-name": "example", "dependency-version": "1.0.1"}], [description])
    assert evaluate(pull, commits)["eligible"] is True


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("1.0.0", "2.0.0"),
        ("1.2.3", "1.2.3"),
        ("1.2.3", "1.2.2"),
        ("1.2.3-rc.1", "1.2.3"),
        ("1.2.3", "1.2.4+build"),
        ("sha256:aaa", "sha256:bbb"),
        ("1.2", "1.3"),
        ("01.2.3", "01.2.4"),
        ("latest", "stable"),
        ("1.2.3", "1.2.1٤"),
    ],
)
def test_major_downgrade_prerelease_digest_and_unknown_versions_stay_manual(old: str, new: str) -> None:
    """An apparent patch label cannot override unsafe or unclassifiable version history."""
    dependency = {
        "dependency-name": "example",
        "dependency-version": new,
        "update-type": "version-update:semver-patch",
    }
    pull, commits = make_case([dependency], [f"Updates `example` from {old} to {new}"])
    assert evaluate(pull, commits)["eligible"] is False


@pytest.mark.parametrize("declared", [None, "unknown", "version-update:semver-major", "version-update:semver-minor"])
def test_inconsistent_declared_classification_stays_manual(declared: object) -> None:
    """Missing types may be inferred, but contradictory or malformed declarations may not."""
    dependency = {"dependency-name": "example", "dependency-version": "1.0.1", "update-type": declared}
    pull, commits = make_case([dependency], ["Updates `example` from 1.0.0 to 1.0.1"])
    assert evaluate(pull, commits)["eligible"] is False


@pytest.mark.parametrize(
    ("target", "path", "value"),
    [
        ("pull", ("user", "login"), "human"),
        ("pull", ("state",), "closed"),
        ("pull", ("draft",), True),
        ("pull", ("head", "repo", "full_name"), "fork/repository"),
        ("pull", ("base", "repo", "full_name"), "other/repository"),
        ("pull", ("base", "ref"), "other"),
        ("pull", ("head", "sha"), "changed-head"),
        ("pull", ("commits",), 1),
        ("commit", ("author", "login"), "human"),
        ("commit", ("committer", "login"), "human-with-valid-signature"),
        ("commit", ("commit", "verification", "verified"), False),
        ("commit", ("commit", "verification", "reason"), "unknown_key"),
    ],
)
def test_human_unsigned_fork_stale_and_incomplete_inputs_are_rejected(
    mixed_case: tuple[ApiObject, list[ApiObject]], target: str, path: tuple[str, ...], value: object
) -> None:
    """Even an older commit in a valid-looking bot PR must pass provenance validation."""
    pull, commits = mixed_case
    selected = pull if target == "pull" else commits[0]
    for key in path[:-1]:
        selected = selected[key]
    selected[path[-1]] = value
    result = evaluate(pull, commits)
    assert result["eligible"] is False
    assert not result["head_sha"]


@pytest.mark.parametrize(
    "description",
    [
        "No complete version history",
        "Updates `example` from 1.0.0 to 1.0.1\nUpdates `example` from 1.0.0 to 1.0.2",
        "Updates `example` from 1.0.0 to 1.0.1\nUpdates `other` from 1.0.0 to 1.0.1",
    ],
)
def test_missing_ambiguous_or_uncovered_dependencies_stay_manual(description: str) -> None:
    """The complete cumulative dependency list must agree with every described version change."""
    pull, commits = make_case([{"dependency-name": "example", "dependency-version": "1.0.1"}], [description])
    assert evaluate(pull, commits)["eligible"] is False


def test_any_major_in_a_mixed_group_blocks_the_entire_group(mixed_case: tuple[ApiObject, list[ApiObject]]) -> None:
    """A Docker major cannot be concealed by the group's three Python patch updates."""
    pull, commits = mixed_case
    commits[-1]["commit"]["message"] = commits[-1]["commit"]["message"].replace("v3.16.0", "v4.0.0")
    assert evaluate(pull, commits)["eligible"] is False


@pytest.mark.parametrize(
    "fragment", ["", "---\nupdated-dependencies: []\n...\n", "---\n!!python/object:unsafe {}\n...\n"]
)
def test_missing_empty_and_unsafe_head_metadata_is_rejected(
    mixed_case: tuple[ApiObject, list[ApiObject]], fragment: str
) -> None:
    """Malformed YAML or absent cumulative head metadata cannot enable a merge."""
    pull, commits = mixed_case
    commits[-1]["commit"]["message"] = fragment
    assert evaluate(pull, commits)["eligible"] is False


def test_duplicate_dependencies_are_ambiguous() -> None:
    """Duplicate package names across directories or ecosystems require manual review."""
    dependency = {"dependency-name": "example", "dependency-version": "1.0.1"}
    pull, commits = make_case([dependency, dependency], ["Updates `example` from 1.0.0 to 1.0.1"])
    assert evaluate(pull, commits)["eligible"] is False


@pytest.mark.parametrize("value", [None, [], {}, 1])
def test_malformed_api_data_never_enables_a_merge(value: object) -> None:
    """Unexpected authenticated API response shapes fail closed."""
    assert evaluate(value, value)["eligible"] is False
