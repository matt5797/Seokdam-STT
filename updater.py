"""Small, dependency-free launcher and updater for Seokdam STT."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

import certifi


LAUNCHER_VERSION = "1.0.1"
MANIFEST_URL = (
    "https://github.com/matt5797/Seokdam-STT/"
    "releases/latest/download/version.json"
)
APP_EXECUTABLE = "Seokdam-STT.exe"
VERSION_FILE = "app-version.json"
UPDATE_DIR = ".update"
HTTP_TIMEOUT_SECONDS = 10
MAX_MANIFEST_BYTES = 64 * 1024


class UpdateError(RuntimeError):
    pass


def install_dir() -> pathlib.Path:
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path(__file__).resolve().parent


def ssl_context() -> ssl.SSLContext:
    """Use the bundled CA bundle when the Windows trust store is incomplete."""
    return ssl.create_default_context(cafile=certifi.where())


def version_key(version: str) -> tuple[int, ...]:
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)", version.strip())
    if not match:
        raise UpdateError(f"잘못된 버전 형식입니다: {version}")
    return tuple(int(part) for part in match.group(1).split("."))


def read_local_version(base_dir: pathlib.Path) -> str | None:
    path = base_dir / VERSION_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return str(data["version"])
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def fetch_json(url: str) -> dict:
    if not url.startswith("https://"):
        raise UpdateError("업데이트 주소는 HTTPS여야 합니다.")
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"Seokdam-Updater/{LAUNCHER_VERSION}"},
    )
    with urllib.request.urlopen(
        request,
        timeout=HTTP_TIMEOUT_SECONDS,
        context=ssl_context(),
    ) as response:
        payload = response.read(MAX_MANIFEST_BYTES + 1)
    if len(payload) > MAX_MANIFEST_BYTES:
        raise UpdateError("업데이트 정보가 허용 크기를 초과했습니다.")
    data = json.loads(payload.decode("utf-8"))
    validate_manifest(data)
    return data


def validate_manifest(manifest: dict) -> None:
    required = {"version", "download_url", "sha256", "executable"}
    if not isinstance(manifest, dict) or not required.issubset(manifest):
        raise UpdateError("업데이트 정보에 필수 항목이 없습니다.")
    version_key(str(manifest["version"]))
    if not str(manifest["download_url"]).startswith("https://"):
        raise UpdateError("다운로드 주소는 HTTPS여야 합니다.")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", str(manifest["sha256"])):
        raise UpdateError("SHA-256 값이 올바르지 않습니다.")
    executable = pathlib.PurePosixPath(str(manifest["executable"]))
    if executable.name != str(executable) or executable.suffix.lower() != ".exe":
        raise UpdateError("실행 파일 이름이 올바르지 않습니다.")
    minimum = manifest.get("min_launcher_version")
    if minimum and version_key(LAUNCHER_VERSION) < version_key(str(minimum)):
        raise UpdateError("업데이터 자체를 새 버전으로 교체해야 합니다.")


def download_file(url: str, destination: pathlib.Path, expected_sha256: str) -> None:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"Seokdam-Updater/{LAUNCHER_VERSION}"},
    )
    digest = hashlib.sha256()
    with urllib.request.urlopen(
        request,
        timeout=HTTP_TIMEOUT_SECONDS,
        context=ssl_context(),
    ) as response:
        with destination.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
    if digest.hexdigest().lower() != expected_sha256.lower():
        destination.unlink(missing_ok=True)
        raise UpdateError("다운로드 파일의 SHA-256 검증에 실패했습니다.")


def safe_extract(archive_path: pathlib.Path, destination: pathlib.Path) -> None:
    destination_root = destination.resolve()
    with zipfile.ZipFile(archive_path) as archive:
        for entry in archive.infolist():
            target = (destination / entry.filename).resolve()
            try:
                target.relative_to(destination_root)
            except ValueError as exc:
                raise UpdateError("ZIP 파일에 안전하지 않은 경로가 있습니다.") from exc
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(entry) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)


def install_package(
    base_dir: pathlib.Path,
    archive_path: pathlib.Path,
    manifest: dict,
) -> None:
    update_root = base_dir / UPDATE_DIR
    update_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=update_root, prefix="staging-") as temp_dir:
        staging = pathlib.Path(temp_dir)
        safe_extract(archive_path, staging)

        executable = str(manifest["executable"])
        required_files = (executable, VERSION_FILE)
        for name in required_files:
            if not (staging / name).is_file():
                raise UpdateError(f"업데이트 패키지에 {name} 파일이 없습니다.")

        backup = update_root / "backup"
        if backup.exists():
            shutil.rmtree(backup)
        backup.mkdir()
        backed_up: list[str] = []
        installed: list[str] = []
        try:
            for name in required_files:
                current = base_dir / name
                if current.exists():
                    os.replace(current, backup / name)
                    backed_up.append(name)
                os.replace(staging / name, current)
                installed.append(name)
        except Exception:
            for name in reversed(installed):
                (base_dir / name).unlink(missing_ok=True)
            for name in reversed(backed_up):
                saved = backup / name
                if saved.exists():
                    os.replace(saved, base_dir / name)
            raise


def update_if_available(base_dir: pathlib.Path) -> bool:
    manifest = fetch_json(MANIFEST_URL)
    current = read_local_version(base_dir)
    if current and version_key(current) >= version_key(str(manifest["version"])):
        print(f"[업데이트] 최신 버전입니다: {current}")
        return False

    update_root = base_dir / UPDATE_DIR
    update_root.mkdir(exist_ok=True)
    archive_path = update_root / "download.zip"
    print(f"[업데이트] {current or '미설치'} -> {manifest['version']} 다운로드 중...")
    try:
        download_file(
            str(manifest["download_url"]),
            archive_path,
            str(manifest["sha256"]),
        )
        install_package(base_dir, archive_path, manifest)
    finally:
        archive_path.unlink(missing_ok=True)
    print(f"[업데이트] {manifest['version']} 설치 완료")
    return True


def launch_app(base_dir: pathlib.Path) -> None:
    executable = base_dir / APP_EXECUTABLE
    if not executable.is_file():
        raise UpdateError(f"실행 파일을 찾을 수 없습니다: {executable}")
    subprocess.Popen([str(executable)], cwd=str(base_dir))


def main() -> int:
    base_dir = install_dir()
    try:
        update_if_available(base_dir)
    except Exception as exc:
        print(f"[업데이트] 확인 실패, 기존 버전을 실행합니다: {exc}")

    try:
        launch_app(base_dir)
        return 0
    except Exception as exc:
        print(f"[실행 실패] {exc}")
        input("Enter 키를 누르면 종료합니다...")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
