import importlib.util
import io
import json
import os
import tarfile
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build_site = load("build_site")
update_app = load("update_app")
SHA_A = "a" * 40
SHA_B = "b" * 40


def archive(entries):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        for name, content in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    data.seek(0)
    return data


class BuildSiteTests(unittest.TestCase):
    def test_rejects_symlink_archive(self):
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as tar:
            info = tarfile.TarInfo("app-sha/assets/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tar.addfile(info)
        data.seek(0)
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                build_site.extract_public_archive(data, Path(temporary), False)

    def test_runtime_install_removes_metadata_and_unpublished_packages(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            def install(*args, **kwargs):
                library = directory / "node_modules/three/build/three.module.js"
                library.parent.mkdir(parents=True)
                library.write_text("export {};")
                extra = directory / "node_modules/unused"
                extra.mkdir()
                (extra / "secret").write_text("do not publish")
            contents = {
                "root/index.html": b"ok",
                "root/app.js": b"ok",
                "root/package.json": b"{}",
                "root/package-lock.json": b"{}",
            }
            with mock.patch.object(build_site.urllib.request, "urlopen", return_value=archive(contents)), mock.patch("subprocess.run", side_effect=install):
                build_site.stage_app("new-app", {"repository": "owner/repo", "ref": SHA_A, "npm_runtime": ["three"]}, directory)
            self.assertTrue((directory / "node_modules/three/build/three.module.js").exists())
            self.assertTrue((directory / "app.js").exists())
            self.assertFalse((directory / "package-lock.json").exists())
            self.assertFalse((directory / "package.json").exists())
            self.assertFalse((directory / "node_modules/unused").exists())

    def test_rejects_malicious_slug_and_ref(self):
        with self.assertRaises(ValueError):
            build_site.validate_manifest({"../escape": {"repository": "a/b", "ref": SHA_A, "title": "x", "description": "x"}})
        with self.assertRaises(ValueError):
            build_site.validate_manifest({"escape": {"repository": "a/b", "ref": "short", "title": "x", "description": "x"}})

    def test_selective_allowlist_and_traversal_rejection(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary)
            build_site.extract_public_archive(archive({
                "app-sha/index.html": b"ok", "app-sha/js/app.js": b"ok",
                "app-sha/assets/cat.png": b"ok", "app-sha/scripts/secret.py": b"no",
                "app-sha/js/app.js.map": b"no", "app-sha/.env": b"no",
            }), destination, False)
            self.assertTrue((destination / "index.html").exists())
            self.assertTrue((destination / "js/app.js").exists())
            self.assertFalse((destination / "scripts").exists())
            self.assertFalse((destination / "js/app.js.map").exists())
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                build_site.extract_public_archive(archive({"app-sha/../outside": b"bad"}), Path(temporary), False)


class UpdateAppTests(unittest.TestCase):
    def test_stale_job_cannot_roll_back_during_retry(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {"CI": "true"}):
            directory = Path(temporary)
            (directory / ".git").mkdir()
            path = self.manifest(directory)
            calls = []
            def run_git(*args, **kwargs):
                calls.append(args)
                if args[0] == "push":
                    raise subprocess.CalledProcessError(1, "git push")
            with mock.patch.object(update_app, "latest_source_ref", side_effect=[SHA_B, SHA_B, "c" * 40]), mock.patch.object(update_app, "git", side_effect=run_git), mock.patch.object(update_app.time, "sleep"):
                # Model fetch/reset restoring the registry on retry.
                original = run_git
                def reset_git(*args, **kwargs):
                    if args[0] == "reset":
                        self.manifest(directory)
                    return original(*args, **kwargs)
                with mock.patch.object(update_app, "git", side_effect=reset_git):
                    self.assertEqual(update_app.update(path, "magicat", "jehyunlee/app_magicat", SHA_B), "stale")
            self.assertEqual(sum(call[0] == "push" for call in calls), 1)

    def manifest(self, directory, ref=SHA_A):
        path = Path(directory) / "apps.json"
        path.write_text(json.dumps({
            "magicat": {"repository": "jehyunlee/app_magicat", "ref": ref, "title": "Magic Cat", "description": "x"},
            "escape": {"repository": "jehyunlee/app_escape", "ref": SHA_B, "title": "Escape", "description": "y"},
        }), encoding="utf-8")
        return path

    def test_noop_does_not_contact_network_or_git(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {"CI": "true"}, clear=False):
            directory = Path(temporary)
            (directory / ".git").mkdir()
            path = self.manifest(directory)
            with mock.patch.object(update_app, "latest_source_ref") as source, mock.patch.object(update_app, "git") as git:
                self.assertEqual(update_app.update(path, "magicat", "jehyunlee/app_magicat", SHA_A), "no-op")
            source.assert_not_called()
            git.assert_not_called()

    def test_update_preserves_peer_manifest_entry(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {"CI": "true"}, clear=False):
            directory = Path(temporary)
            (directory / ".git").mkdir()
            path = self.manifest(directory)
            with mock.patch.object(update_app, "latest_source_ref", return_value=SHA_B), mock.patch.object(update_app, "git"):
                self.assertEqual(update_app.update(path, "magicat", "jehyunlee/app_magicat", SHA_B), "updated")
            result = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(result["magicat"]["ref"], SHA_B)
            self.assertEqual(result["escape"]["ref"], SHA_B)
            self.assertEqual(result["escape"]["title"], "Escape")

    def test_bad_mapping_fails_before_mutation(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, {"CI": "true"}, clear=False):
            directory = Path(temporary)
            (directory / ".git").mkdir()
            path = self.manifest(directory)
            with self.assertRaises(update_app.UpdateError):
                update_app.update(path, "magicat", "jehyunlee/app_escape", SHA_B)


if __name__ == "__main__":
    unittest.main()
