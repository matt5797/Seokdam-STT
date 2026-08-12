import hashlib
import json
import pathlib
import tempfile
import unittest
import zipfile

import updater


class UpdaterTests(unittest.TestCase):
    def test_version_key_compares_numeric_versions(self):
        self.assertGreater(updater.version_key("1.10.0"), updater.version_key("1.2.9"))
        self.assertEqual(updater.version_key("v2.0.0"), (2, 0, 0))

    def test_manifest_rejects_non_https_download(self):
        manifest = {
            "version": "1.2.0",
            "download_url": "http://example.com/app.zip",
            "sha256": "a" * 64,
            "executable": "Seokdam-STT.exe",
        }
        with self.assertRaises(updater.UpdateError):
            updater.validate_manifest(manifest)

    def test_safe_extract_rejects_parent_path(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("../outside.txt", "bad")
            with self.assertRaises(updater.UpdateError):
                updater.safe_extract(archive, root / "extract")

    def test_install_package_replaces_app_and_preserves_env(self):
        with tempfile.TemporaryDirectory() as temp:
            root = pathlib.Path(temp)
            (root / updater.APP_EXECUTABLE).write_bytes(b"old")
            (root / updater.VERSION_FILE).write_text(
                json.dumps({"version": "1.0.0"}), encoding="utf-8"
            )
            (root / ".env").write_text("SECRET=keep", encoding="utf-8")
            archive = root / "release.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr(updater.APP_EXECUTABLE, b"new")
                output.writestr(updater.VERSION_FILE, json.dumps({"version": "1.1.0"}))
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            manifest = {
                "version": "1.1.0",
                "download_url": "https://example.com/release.zip",
                "sha256": digest,
                "executable": updater.APP_EXECUTABLE,
            }

            updater.install_package(root, archive, manifest)

            self.assertEqual((root / updater.APP_EXECUTABLE).read_bytes(), b"new")
            self.assertEqual(updater.read_local_version(root), "1.1.0")
            self.assertEqual((root / ".env").read_text(encoding="utf-8"), "SECRET=keep")


if __name__ == "__main__":
    unittest.main()
