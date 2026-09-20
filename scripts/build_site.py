#!/usr/bin/env python3
"""Build the pinned static application site described by an apps manifest."""
import argparse
import json
import os
import re
import shutil
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
SITE_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(?:/[a-z0-9][a-z0-9_-]*)*$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
NPM_RE = re.compile(r"^(?:@[a-z0-9-]+/)?[a-z0-9][a-z0-9._-]*$")
MAX_FILE_SIZE = 100 * 1024 * 1024
MAX_ARCHIVE_SIZE = 1024 * 1024 * 1024


def fail(message):
    raise ValueError(message)


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or not manifest:
        fail("manifest must be a non-empty object")
    paths = []
    for slug, app in manifest.items():
        if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
            fail("invalid app slug")
        if not isinstance(app, dict):
            fail("invalid app entry")
        target = app.get("path", slug)
        if not isinstance(target, str) or not SITE_PATH_RE.fullmatch(target):
            fail("invalid public path for " + slug)
        if any(target == prior or target.startswith(prior + "/") or prior.startswith(target + "/") for prior in paths):
            fail("overlapping public paths")
        paths.append(target)
        public_dirs = app.get("public_dirs", [])
        if not isinstance(public_dirs, list) or any(
            not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", name)
            or name in {"tests", "scripts", "node_modules"} for name in public_dirs
        ):
            fail("invalid public directory list")
        repository, ref = app.get("repository"), app.get("ref")
        if not isinstance(repository, str) or not REPOSITORY_RE.fullmatch(repository):
            fail("invalid repository for " + slug)
        if not isinstance(ref, str) or not SHA_RE.fullmatch(ref):
            fail("invalid ref for " + slug)
        if not isinstance(app.get("title"), str) or not isinstance(app.get("description"), str):
            fail("title and description are required for " + slug)
        dependencies = app.get("npm_runtime", [])
        if not isinstance(dependencies, list) or any(
            not isinstance(name, str) or not NPM_RE.fullmatch(name) for name in dependencies
        ):
            fail("invalid runtime dependency list for " + slug)


def safe_member_path(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or any(part in ("", ".") for part in path.parts):
        fail("unsafe archive path")
    return path


def public_path(relative, escape, public_dirs=()):
    parts = relative.parts
    if not parts:
        return False
    if any(part.startswith(".") for part in parts) or relative.suffix == ".map":
        return False
    if len(parts) == 1:
        if relative.name in ("index.html", "manifest.webmanifest"):
            return True
        return relative.suffix in (".js", ".css", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico")
    return parts[0] in {"css", "js", "assets", *public_dirs}


def extract_public_archive(response, destination, escape, public_dirs=()):
    """Copy only allowed regular files from a GitHub tarball without extraction."""
    total = 0
    root = None
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=response, mode="r|gz") as archive:
        for member in archive:
            path = safe_member_path(member.name)
            if root is None:
                root = path.parts[0]
            if path.parts[0] != root:
                fail("archive has multiple roots")
            if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                fail("archive contains non-regular entry")
            if member.isdir():
                continue
            if not member.isreg():
                fail("archive contains unsupported entry")
            if member.size > MAX_FILE_SIZE:
                fail("archive file exceeds size limit")
            total += member.size
            if total > MAX_ARCHIVE_SIZE:
                fail("archive exceeds size limit")
            if len(path.parts) < 2:
                continue
            relative = PurePosixPath(*path.parts[1:])
            # package metadata is needed transiently for escape's locked install.
            keep = public_path(relative, escape, public_dirs) or (escape and relative.name in {"package.json", "package-lock.json"} and len(relative.parts) == 1)
            if not keep:
                continue
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                fail("unable to read archive member")
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
    if root is None:
        fail("empty archive")


def copy_tree(source, destination):
    for path in source.rglob("*"):
        if path.is_symlink():
            fail("dependency tree contains symlink")
        if path.is_file():
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def stage_app(slug, app, app_directory):
    runtime = app.get("npm_runtime", [])
    url = "https://codeload.github.com/{}/tar.gz/{}".format(app["repository"], app["ref"])
    with urllib.request.urlopen(url, timeout=60) as response:
        extract_public_archive(response, app_directory, bool(runtime), app.get("public_dirs", []))
    if not (app_directory / "index.html").is_file():
        fail("missing index.html for " + slug)
    if runtime:
        package_json = app_directory / "package.json"
        package_lock = app_directory / "package-lock.json"
        if not package_json.is_file() or not package_lock.is_file():
            fail(slug + " requires package.json and package-lock.json")
        import subprocess
        subprocess.run(["npm", "ci", "--ignore-scripts"], cwd=app_directory, check=True)
        publish_modules = app_directory / "_publish_node_modules"
        for dependency in runtime:
            installed = app_directory / "node_modules" / dependency
            if not installed.is_dir():
                fail(slug + " lockfile did not install " + dependency)
            copy_tree(installed, publish_modules / dependency)
        shutil.rmtree(app_directory / "node_modules")
        package_json.unlink()
        package_lock.unlink()
        publish_modules.rename(app_directory / "node_modules")


def safe_output_path(output, manifest_path):
    output = output.resolve()
    if output.name != "_site" or output == Path(output.anchor) or output in (Path.cwd().resolve(), manifest_path.parent.resolve()):
        fail("unsafe output path")
    return output


def replace_output(temp_output, output):
    backup = output.with_name(output.name + ".previous")
    if backup.exists():
        shutil.rmtree(backup)
    if output.exists():
        output.rename(backup)
    try:
        temp_output.rename(output)
    except Exception:
        if backup.exists():
            backup.rename(output)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def build(manifest_path, output):
    manifest_path = manifest_path.resolve()
    with manifest_path.open(encoding="utf-8") as handle:
        manifest = json.load(handle)
    validate_manifest(manifest)
    output = safe_output_path(output, manifest_path)
    hub = manifest_path.parent
    hub_index = hub / "index.html"
    if not hub_index.is_file():
        fail("hub index.html is required")
    temp_root = Path(tempfile.mkdtemp(prefix="site-build-", dir=output.parent))
    temp_output = temp_root / "_site"
    try:
        temp_output.mkdir()
        shutil.copy2(hub_index, temp_output / "index.html")
        for name in ("CNAME", ".nojekyll"):
            source = hub / name
            if source.is_file():
                shutil.copy2(source, temp_output / name)
        for index in (hub / "collections").glob("*/index.html"):
            category = index.parent.name
            if index.is_symlink() or index.parent.is_symlink() or not SITE_PATH_RE.fullmatch(category):
                fail("invalid collection index")
            if any(app.get("path", slug) == category or category.startswith(app.get("path", slug) + "/") for slug, app in manifest.items()):
                fail("collection overlaps an app")
            (temp_output / category).mkdir(parents=True, exist_ok=True)
            shutil.copy2(index, temp_output / category / "index.html")
        for slug, app in manifest.items():
            stage_app(slug, app, temp_output / app.get("path", slug))
        replace_output(temp_output, output)
    finally:
        if temp_root.exists():
            shutil.rmtree(temp_root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        build(args.manifest, args.output)
    except (OSError, ValueError, tarfile.TarError, json.JSONDecodeError) as error:
        parser.exit(1, "build_site: {}\n".format(error))


if __name__ == "__main__":
    main()
