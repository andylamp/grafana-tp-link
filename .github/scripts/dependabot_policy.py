# Copyright (c) 2026 grafana-tp-link contributors
# SPDX-License-Identifier: GPL-3.0-only
# ruff: file-ignore[implicit-namespace-package] - Standalone workflow script, not an importable package.

"""Classify authenticated Dependabot PR metadata without executing pull-request code."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import cast

import yaml

BOT = "dependabot[bot]"
STABLE_VERSION = re.compile(r"v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
METADATA = re.compile(r"^---\r?\n(.*?)^\.\.\.[ \t]*\r?$", re.MULTILINE | re.DOTALL)
VERSION_PAIRS = (
    re.compile(r"^Updates `(?P<name>[^`\n]+)` from (?P<old>\S+) to (?P<new>\S+)$", re.MULTILINE),
    re.compile(r"^Bumps \[(?P<name>[^\]\n]+)\]\([^\s]+\) from (?P<old>\S+) to (?P<new>\S+)\.$", re.MULTILINE),
    re.compile(r"^Bumps (?P<name>[^\s`\[]+) from (?P<old>\S+) to (?P<new>\S+)\.$", re.MULTILINE),
)


class IneligibleError(ValueError):
    """Describe a condition that requires manual review instead of automatic merging."""


def _require(condition: object, reason: str) -> None:
    """Reject a policy condition with a fixed, credential-free explanation.

    Raises
    ------
    IneligibleError
        If the required condition is false.
    """
    if not condition:
        raise IneligibleError(reason)


def _mapping(value: object) -> dict[str, object]:
    """Require an object from authenticated API data or safe YAML.

    Returns
    -------
    dict[str, object]
        The checked mapping.
    """
    _require(isinstance(value, dict), "Malformed metadata object.")
    return cast("dict[str, object]", value)


def _version(value: object) -> tuple[int, int, int]:
    """Read only stable three-part numeric versions, optionally prefixed with v.

    Returns
    -------
    tuple[int, int, int]
        Numeric release components.

    Raises
    ------
    IneligibleError
        If the version is missing, ambiguous, or not a stable numeric release.
    """
    match = STABLE_VERSION.fullmatch(value) if isinstance(value, str) else None
    if match is None:
        message = "Missing or nonstandard version; manual review required."
        raise IneligibleError(message)
    return int(match[1]), int(match[2]), int(match[3])


def _verified_messages(pull: object, commits: object, repository: str, base_branch: str) -> tuple[str, list[str]]:
    """Verify provenance of the complete commit list against the current pull request.

    Returns
    -------
    tuple[str, list[str]]
        Authenticated head SHA and ordered commit messages.
    """
    pr = _mapping(pull)
    head, base = _mapping(pr["head"]), _mapping(pr["base"])
    _require(_mapping(pr["user"])["login"] == BOT, "Pull request author is not Dependabot.")
    _require(pr["state"] == "open" and pr["draft"] is False, "Pull request must be open and ready for review.")
    _require(
        _mapping(head["repo"])["full_name"] == repository
        and _mapping(base["repo"])["full_name"] == repository
        and base["ref"] == base_branch,
        "Pull request must target the expected branch within this repository.",
    )
    _require(isinstance(commits, list) and bool(commits), "Complete commit metadata is required.")
    records = [_mapping(commit) for commit in cast("list[object]", commits)]
    _require(type(pr["commits"]) is int and pr["commits"] == len(records), "Commit count does not match the PR.")
    sha = head["sha"]
    _require(isinstance(sha, str) and records[-1]["sha"] == sha, "Commit list does not match the current PR head.")
    messages = []
    for record in records:
        commit = _mapping(record["commit"])
        verification = _mapping(commit["verification"])
        _require(
            _mapping(record["author"])["login"] == BOT
            and _mapping(record["committer"])["login"] in {BOT, "web-flow"}
            and verification["verified"] is True
            and verification["reason"] == "valid",
            "Every commit must have verified Dependabot provenance.",
        )
        _require(isinstance(commit["message"], str), "Missing commit message.")
        messages.append(cast("str", commit["message"]))
    return cast("str", sha), messages


def _validate_dependencies(messages: list[str]) -> None:
    """Require complete, unambiguous metadata proving stable patch/minor upgrades."""
    fragments = METADATA.findall(messages[-1])
    _require(len(fragments) == 1, "One cumulative metadata block is required on the head commit.")
    dependencies = _mapping(yaml.safe_load(fragments[0]))["updated-dependencies"]
    _require(isinstance(dependencies, list) and bool(dependencies), "Dependency metadata must not be empty.")
    pairs: dict[str, set[tuple[str, str]]] = {}
    for message in messages:
        for pattern in VERSION_PAIRS:
            for match in pattern.finditer(message):
                pairs.setdefault(match["name"], set()).add((match["old"], match["new"]))
    names = set()
    for raw in cast("list[object]", dependencies):
        dependency = _mapping(raw)
        name = dependency["dependency-name"]
        _require(isinstance(name, str) and bool(name), "Dependency name is missing.")
        name = cast("str", name)
        _require(name not in names, "Duplicate dependencies require manual review.")
        names.add(name)
        versions = pairs.get(name, set())
        _require(len(versions) == 1, "Missing or ambiguous version history requires manual review.")
        old, new = (_version(value) for value in next(iter(versions)))
        _require(new == _version(dependency["dependency-version"]), "Version metadata is inconsistent.")
        _require(new > old, "Downgrades or unchanged versions require manual review.")
        _require(new[0] == old[0], "Major updates require manual review.")
        classification = "version-update:semver-minor" if new[1] != old[1] else "version-update:semver-patch"
        _require(
            dependency.get("update-type", classification) == classification,
            "Update classification is unknown or inconsistent.",
        )
    _require(names == pairs.keys(), "Cumulative metadata does not cover every described dependency.")


def evaluate(
    pull: object,
    commits: object,
    *,
    repository: str = "andylamp/grafana-tp-link",
    base_branch: str = "master",
) -> dict[str, bool | str]:
    """Return a conservative decision from REST pull-request and complete commit responses.

    Returns
    -------
    dict[str, bool | str]
        Eligibility, a fixed explanation, and the eligible head SHA or an empty string.
    """
    try:
        sha, messages = _verified_messages(pull, commits, repository, base_branch)
        _validate_dependencies(messages)
    except IneligibleError as error:
        return {"eligible": False, "reason": str(error), "head_sha": ""}
    except (KeyError, TypeError, ValueError, yaml.YAMLError):
        return {
            "eligible": False,
            "reason": "Malformed or incomplete metadata requires manual review.",
            "head_sha": "",
        }
    return {"eligible": True, "reason": "Every dependency is a stable patch or minor upgrade.", "head_sha": sha}


def main() -> None:
    """Read authenticated JSON files and print the merge decision without mutating GitHub."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pull", type=Path)
    parser.add_argument("commits", type=Path)
    arguments = parser.parse_args()
    result = evaluate(
        json.loads(arguments.pull.read_text(encoding="utf-8")),
        json.loads(arguments.commits.read_text(encoding="utf-8")),
        repository=os.getenv("GH_REPO", "andylamp/grafana-tp-link"),
        base_branch=os.getenv("DEFAULT_BRANCH", "master"),
    )
    sys.stdout.write(json.dumps(result) + "\n")


if __name__ == "__main__":
    main()
