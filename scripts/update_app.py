#!/usr/bin/env python3
"""CI-only pinned-ref updater. It resets its dedicated checkout before each retry."""
import argparse
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_PUSH_ATTEMPTS = 5


class UpdateError(RuntimeError):
    pass


def validate_argument(slug, repository, ref):
    if not SLUG_RE.fullmatch(slug):
        raise UpdateError("invalid app slug")
    if not REPOSITORY_RE.fullmatch(repository):
        raise UpdateError("invalid repository")
    if not SHA_RE.fullmatch(ref):
        raise UpdateError("ref must be a full lowercase SHA")


def read_manifest(path):
    with path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    if not isinstance(manifest, dict):
        raise UpdateError("manifest must be an object")
    return manifest


def validate_mapping(manifest, slug, repository):
    app = manifest.get(slug)
    if not isinstance(app, dict) or app.get("repository") != repository:
        raise UpdateError("app and repository mapping does not match manifest")
    current = app.get("ref")
    if not isinstance(current, str) or not SHA_RE.fullmatch(current):
        raise UpdateError("manifest contains invalid pinned ref")
    return current


def latest_source_ref(repository):
    request = urllib.request.Request("https://api.github.com/repos/{}/commits/main".format(repository), headers={"Accept": "application/vnd.github+json"})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.load(response)
    sha = payload.get("sha") if isinstance(payload, dict) else None
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
        raise UpdateError("GitHub returned no full source SHA")
    return sha


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True)


def write_manifest(path, manifest):
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    temporary.replace(path)


def update(manifest_path, slug, repository, ref):
    validate_argument(slug, repository, ref)
    if os.environ.get("CI") != "true":
        raise UpdateError("update_app.py may run only in a dedicated CI checkout")
    manifest_path = manifest_path.resolve()
    checkout = manifest_path.parent
    if not (checkout / ".git").exists():
        raise UpdateError("manifest must be in a git checkout")

    initial = read_manifest(manifest_path)
    if validate_mapping(initial, slug, repository) == ref:
        return "no-op"
    if latest_source_ref(repository) != ref:
        return "stale"

    for attempt in range(MAX_PUSH_ATTEMPTS):
        git("fetch", "origin", "main", cwd=checkout)
        # This checkout is dedicated CI workspace; discard only its retry state.
        git("reset", "--hard", "origin/main", cwd=checkout)
        manifest = read_manifest(manifest_path)
        current = validate_mapping(manifest, slug, repository)
        if current == ref:
            return "no-op"
        if latest_source_ref(repository) != ref:
            return "stale"
        manifest[slug]["ref"] = ref
        write_manifest(manifest_path, manifest)
        git("add", "apps.json", cwd=checkout)
        git("commit", "-m", "chore: update {} to {}".format(slug, ref[:12]), cwd=checkout)
        try:
            git("push", "origin", "HEAD:main", cwd=checkout)
            return "updated"
        except subprocess.CalledProcessError:
            if attempt == MAX_PUSH_ATTEMPTS - 1:
                raise UpdateError("push failed after {} attempts".format(MAX_PUSH_ATTEMPTS))
            time.sleep(attempt + 1)
    raise UpdateError("unreachable retry state")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--manifest", type=Path, default=Path("apps.json"))
    args = parser.parse_args()
    try:
        result = update(args.manifest, args.app, args.repository, args.ref)
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError, subprocess.CalledProcessError, UpdateError) as error:
        parser.exit(1, "update_app: {}\n".format(error))
    print(result)


if __name__ == "__main__":
    main()
