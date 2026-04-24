# SPDX-FileCopyrightText: 2026 github.com/aburow
# SPDX-License-Identifier: GPL-3.0-only

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path

LOG = logging.getLogger("ups2mqtt.updater")

DEFAULT_REPOS: dict[str, str] = {
    "apc-modbus-ha": "https://github.com/aburow/apc-modbus-snmp-ha.git",
    "ups-snmp-ha": "https://github.com/aburow/ups-snmp-ha.git",
    "cyberpower-modbus-ha": "https://github.com/aburow/cyberpower-modbus-ha.git",
}


def _run(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    completed = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=180,
        check=False,
    )
    return completed.returncode, completed.stdout.strip()


def _get_cache_path(apps_dir: str) -> Path:
    """Get the path to the version cache file."""
    return Path(apps_dir).parent / ".version_cache.json"


def _load_cache(apps_dir: str) -> dict[str, str]:
    """Load SHA-to-release mappings from cache."""
    cache_path = _get_cache_path(apps_dir)
    if cache_path.exists():
        try:
            with cache_path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError, ValueError) as err:
            LOG.warning("Failed to load version cache: %s", err)
    return {}


def _save_cache(apps_dir: str, cache: dict[str, str]) -> None:
    """Save SHA-to-release mappings to cache."""
    cache_path = _get_cache_path(apps_dir)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        with cache_path.open("w", encoding="utf-8") as f:
            json.dump(cache, f)
    except OSError as err:
        LOG.warning("Failed to save version cache: %s", err)


def _env_repo_url(name: str, default: str) -> str:
    env_name = f"UPS_UNIFIED_REPO_{name.upper().replace('-', '_')}_URL"
    return os.environ.get(env_name, default).strip() or default


def _current_sha(path: Path) -> str:
    code, out = _run(["git", "rev-parse", "--short", "HEAD"], cwd=path)
    return out if code == 0 else "unknown"


def _remote_sha(path: Path) -> str:
    code, out = _run(["git", "rev-parse", "--short", "origin/main"], cwd=path)
    return out if code == 0 else "unknown"


def _is_prerelease(version: str) -> bool:
    """Check if version is a pre-release (alpha, beta, rc, dev, etc)."""
    version_lower = version.lower()
    return any(
        marker in version_lower
        for marker in ["-alpha", "-beta", "-rc", "-dev", "-pre", "-a", "-b"]
    )


def _parse_semver(tag: str) -> tuple[int, int, int, bool, int, int, str] | None:
    """Parse semantic version from tag. Returns sortable semver tuple."""
    tag_normalized = tag.lstrip("v")
    match = re.match(r"(\d+)\.(\d+)\.(\d+)(.*)", tag_normalized)
    if not match:
        return None
    major, minor, patch, suffix = match.groups()
    is_pre = _is_prerelease(tag_normalized)

    # Extract prerelease stage/sequence so 1.2.3-dev.19 sorts after 1.2.3-dev.18.
    prerelease_stage_rank = -1
    prerelease_seq = -1
    suffix_lower = suffix.lower()
    prerelease_match = re.search(
        r"(?:^|[-._])(dev|alpha|beta|rc|pre|a|b)(?:[-._]?(\d+))?",
        suffix_lower,
    )
    if prerelease_match:
        marker = prerelease_match.group(1)
        marker_aliases = {"a": "alpha", "b": "beta", "pre": "beta"}
        marker = marker_aliases.get(marker, marker)
        stage_order = {"dev": 0, "alpha": 1, "beta": 2, "rc": 3}
        prerelease_stage_rank = stage_order.get(marker, -1)
        prerelease_seq = int(prerelease_match.group(2) or "0")

    return (
        int(major),
        int(minor),
        int(patch),
        is_pre,
        prerelease_stage_rank,
        prerelease_seq,
        suffix_lower,
    )


def get_releases(repo_url: str) -> dict[str, list[str]]:
    """Get stable and pre-release versions from git repo tags."""
    code, out = _run(["git", "ls-remote", "--tags", repo_url])
    if code != 0:
        return {"stable": [], "prerelease": []}

    stable = []
    prerelease = []

    for line in out.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        tag_ref = parts[1]
        if "^{}" in tag_ref:
            continue
        tag = tag_ref.replace("refs/tags/", "")
        parsed = _parse_semver(tag)
        if parsed is None:
            continue
        if "^" not in tag:
            if parsed[3]:
                prerelease.append(tag)
            else:
                stable.append(tag)

    stable.sort(
        key=lambda t: _parse_semver(t) or (0, 0, 0, False, -1, -1, ""),
        reverse=True,
    )
    prerelease.sort(
        key=lambda t: _parse_semver(t) or (0, 0, 0, False, -1, -1, ""),
        reverse=True,
    )

    return {"stable": stable, "prerelease": prerelease}


def _sha_to_release(cache: dict[str, str], sha: str) -> str:
    """Find the release tag for a given commit SHA using cache."""
    if not sha or sha in {"unknown", "not-synced"}:
        return sha
    if sha in cache:
        return cache[sha]
    return sha  # Return SHA if not in cache


def _installed_ref_to_release(path: Path, cache: dict[str, str]) -> str:
    """Resolve installed app display version to a tag when possible."""
    installed_sha = _current_sha(path)
    release = _sha_to_release(cache, installed_sha)
    if release != installed_sha:
        return release
    # Fallback: resolve local checked-out tag without relying on remote cache.
    code, out = _run(["git", "describe", "--tags", "--exact-match", "HEAD"], cwd=path)
    local_tag = out.strip()
    if code == 0 and local_tag and _parse_semver(local_tag) is not None:
        return local_tag
    return installed_sha


def _build_sha_to_release_cache(repo_url: str) -> dict[str, str]:
    """Build SHA-to-release mapping from git ls-remote output."""
    mapping = {}
    code, out = _run(["git", "ls-remote", "--tags", repo_url])
    if code != 0:
        return mapping

    # First pass: collect all tags and their commit SHAs
    # For each tag: prefer the ^{} line (actual commit) over the tag object
    tag_to_sha = {}
    for line in out.split("\n"):
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        sha = parts[0]
        tag_ref = parts[1]

        # Extract tag name and whether it's an annotated tag pointer
        tag = tag_ref.replace("refs/tags/", "").replace("^{}", "")
        is_commit_sha = "^{}" in tag_ref

        # Only map valid semver tags
        if _parse_semver(tag) is not None:
            # Prefer annotated tag pointers (^{}) as they point to commits
            if is_commit_sha or tag not in tag_to_sha:
                tag_to_sha[tag] = sha

    # Second pass: map SHAs to tags
    for tag, sha in tag_to_sha.items():
        mapping[sha] = tag
        # Map common short SHA lengths to support varied local git abbrev settings.
        for length in range(7, min(12, len(sha)) + 1):
            mapping[sha[:length]] = tag

    return mapping


def refresh_version_cache(apps_dir: str) -> None:
    """Refresh the SHA-to-release version cache for all apps."""
    cache: dict[str, str] = {}
    for app_name, default_url in DEFAULT_REPOS.items():
        repo_url = _env_repo_url(app_name, default_url)
        app_cache = _build_sha_to_release_cache(repo_url)
        cache.update(app_cache)
        LOG.info(
            "Refreshed version cache for %s: %d mappings", app_name, len(app_cache)
        )
    _save_cache(apps_dir, cache)
    LOG.info("Version cache updated: %d total mappings", len(cache))


def get_app_versions(apps_dir: str) -> dict[str, dict[str, str]]:
    """Get installed and remote versions for each app (uses cache)."""
    root = Path(apps_dir)
    cache = _load_cache(apps_dir)
    versions: dict[str, dict[str, str]] = {}
    for name in DEFAULT_REPOS.keys():
        target = root / name
        if (target / ".git").exists():
            installed_sha = _current_sha(target)
            remote_sha = _remote_sha(target)
            installed_release = _installed_ref_to_release(target, cache)
            remote_release = _sha_to_release(cache, remote_sha)
            versions[name] = {
                "installed": installed_release,
                "remote": remote_release,
                "status": "up-to-date"
                if installed_sha == remote_sha
                else "update-available",
            }
        else:
            versions[name] = {
                "installed": "not-synced",
                "remote": "unknown",
                "status": "not-synced",
            }
    return versions


def sync_sources(
    apps_dir: str,
    branch: str = "main",
    app_name: str | None = None,
    release: str | None = None,
) -> dict[str, dict[str, str | bool]]:
    branch = os.environ.get("UPS_UNIFIED_REPO_BRANCH", branch).strip() or branch
    root = Path(apps_dir)
    root.mkdir(parents=True, exist_ok=True)
    results: dict[str, dict[str, str | bool]] = {}

    repos_to_sync = (
        {app_name: DEFAULT_REPOS[app_name]}
        if app_name and app_name in DEFAULT_REPOS
        else DEFAULT_REPOS
    )

    for name, default_url in repos_to_sync.items():
        repo_url = _env_repo_url(name, default_url)
        target = root / name
        checkout_ref = release or branch
        try:
            if (target / ".git").exists():
                before = _current_sha(target)
                code_fetch, out_fetch = _run(
                    ["git", "fetch", "--prune", "--tags", "origin"], cwd=target
                )
                if code_fetch != 0:
                    results[name] = {"ok": False, "message": out_fetch}
                    continue
                if release:
                    # Checkout to specific release tag
                    code_checkout, out_checkout = _run(
                        ["git", "checkout", release], cwd=target
                    )
                    if code_checkout != 0:
                        results[name] = {"ok": False, "message": out_checkout}
                        continue
                else:
                    # Checkout and reset to branch (handles diverged branches)
                    code_checkout, out_checkout = _run(
                        ["git", "checkout", branch], cwd=target
                    )
                    if code_checkout != 0:
                        results[name] = {"ok": False, "message": out_checkout}
                        continue
                    code_reset, out_reset = _run(
                        ["git", "reset", "--hard", f"origin/{branch}"], cwd=target
                    )
                    if code_reset != 0:
                        results[name] = {"ok": False, "message": out_reset}
                        continue
                after = _current_sha(target)
                results[name] = {
                    "ok": True,
                    "changed": before != after,
                    "before": before,
                    "after": after,
                    "message": (
                        f"checked out {release}"
                        if release
                        else f"synced to origin/{branch}"
                    ),
                }
            else:
                code_clone, out_clone = _run(
                    [
                        "git",
                        "clone",
                        "--depth",
                        "1",
                        "--branch",
                        checkout_ref,
                        repo_url,
                        str(target),
                    ]
                )
                if code_clone != 0:
                    results[name] = {"ok": False, "message": out_clone}
                    continue
                after = _current_sha(target)
                results[name] = {
                    "ok": True,
                    "changed": True,
                    "before": "-",
                    "after": after,
                    "message": f"cloned {checkout_ref}",
                }
        except (OSError, subprocess.SubprocessError) as err:
            results[name] = {"ok": False, "message": str(err)}

    return results
