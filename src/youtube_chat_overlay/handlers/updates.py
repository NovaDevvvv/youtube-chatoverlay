"""GitHub Releases update discovery, download, and Windows installation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import requests
from PySide6.QtCore import QThread, Signal

from ..config import (
    APP_NAME,
    APP_VERSION,
    GITHUB_API_ROOT,
    GITHUB_REPOSITORY,
    UPDATE_ASSET_NAME,
)


@dataclass(slots=True)
class ReleaseInfo:
    version: str
    title: str
    notes: str
    page_url: str
    download_url: str
    checksum_url: str | None = None


def _version_tuple(value: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", value.lstrip("vV").split("-", 1)[0])
    return tuple(int(number) for number in numbers) or (0,)


def discover_repository(app_dir: Path) -> str:
    configured = (os.getenv("YOUTUBE_OVERLAY_GITHUB_REPOSITORY") or GITHUB_REPOSITORY).strip()
    if configured:
        return configured.removesuffix(".git").strip("/")

    config = app_dir / ".git" / "config"
    if config.is_file():
        match = re.search(
            r"url\s*=\s*(?:https://github\.com/|git@github\.com:)([^\s]+)",
            config.read_text(encoding="utf-8", errors="ignore"),
        )
        if match:
            return match.group(1).removesuffix(".git").strip("/")
    return ""


class UpdateError(RuntimeError):
    pass


class UpdateService:
    def __init__(self, app_dir: Path):
        self.app_dir = app_dir.resolve()
        self.repository = discover_repository(self.app_dir)
        self.session = requests.Session()
        self.session.headers.update(
            {"Accept": "application/vnd.github+json", "User-Agent": f"{APP_NAME}/{APP_VERSION}"}
        )

    def latest_release(self) -> ReleaseInfo | None:
        if not self.repository:
            raise UpdateError(
                "No GitHub repository is configured. Set GITHUB_REPOSITORY in app_config.py "
                "to owner/repository before distributing the app."
            )
        response = self.session.get(
            f"{GITHUB_API_ROOT}/repos/{self.repository}/releases/latest", timeout=12
        )
        if response.status_code == 404:
            raise UpdateError("This repository does not have a published GitHub Release yet.")
        response.raise_for_status()
        data = response.json()
        version = str(data.get("tag_name") or "").lstrip("vV")
        if not version or _version_tuple(version) <= _version_tuple(APP_VERSION):
            return None

        assets = data.get("assets") or []
        package = next((a for a in assets if a.get("name") == UPDATE_ASSET_NAME), None)
        if not package:
            raise UpdateError(
                f"Release v{version} is missing the required {UPDATE_ASSET_NAME} asset."
            )
        checksum = next(
            (a for a in assets if a.get("name") in {UPDATE_ASSET_NAME + ".sha256", "SHA256SUMS"}),
            None,
        )
        return ReleaseInfo(
            version=version,
            title=str(data.get("name") or f"Version {version}"),
            notes=str(data.get("body") or "No release notes were provided."),
            page_url=str(data.get("html_url") or ""),
            download_url=str(package["browser_download_url"]),
            checksum_url=str(checksum["browser_download_url"]) if checksum else None,
        )

    def download(self, release: ReleaseInfo, progress) -> Path:
        target_dir = Path(tempfile.mkdtemp(prefix="youtube-overlay-update-"))
        archive = target_dir / UPDATE_ASSET_NAME
        with self.session.get(release.download_url, stream=True, timeout=30) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0))
            received = 0
            with archive.open("wb") as output:
                for chunk in response.iter_content(1024 * 256):
                    if chunk:
                        output.write(chunk)
                        received += len(chunk)
                        progress(int(received * 100 / total) if total else 0)

        if release.checksum_url:
            checksum_response = self.session.get(release.checksum_url, timeout=12)
            checksum_response.raise_for_status()
            expected = re.search(r"\b[a-fA-F0-9]{64}\b", checksum_response.text)
            actual = hashlib.sha256(archive.read_bytes()).hexdigest()
            if not expected or actual.lower() != expected.group(0).lower():
                shutil.rmtree(target_dir, ignore_errors=True)
                raise UpdateError("The downloaded update failed its SHA-256 integrity check.")

        stage = target_dir / "stage"
        stage.mkdir()
        with zipfile.ZipFile(archive) as bundle:
            root = stage.resolve()
            for member in bundle.infolist():
                destination = (stage / member.filename).resolve()
                if root not in destination.parents and destination != root:
                    raise UpdateError("The update archive contains an unsafe path.")
            bundle.extractall(stage)
        children = list(stage.iterdir())
        return children[0] if len(children) == 1 and children[0].is_dir() else stage

    def launch_installer(self, staged_dir: Path) -> None:
        script = staged_dir.parent / "install-update.ps1"
        app_dir = str(self.app_dir).replace("'", "''")
        source = str(staged_dir).replace("'", "''")
        executable = str(Path(sys.executable)).replace("'", "''")
        source_root = str(self.app_dir / "src").replace("'", "''")
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue\n"
            f"Copy-Item -Path '{source}\\*' -Destination '{app_dir}' -Recurse -Force\n"
            f"& '{executable}' -m pip install -r '{app_dir}\\requirements.txt' --disable-pip-version-check\n"
            f"$env:PYTHONPATH = '{source_root}'\n"
            f"Start-Process -FilePath '{executable}' -ArgumentList @('-m', 'youtube_chat_overlay') -WorkingDirectory '{app_dir}'\n",
            encoding="utf-8-sig",
        )
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0),
        )


class UpdateCheckWorker(QThread):
    found = Signal(object)
    current = Signal()
    failed = Signal(str)

    def __init__(self, service: UpdateService, parent=None):
        super().__init__(parent)
        self.service = service

    def run(self) -> None:
        try:
            release = self.service.latest_release()
            self.current.emit() if release is None else self.found.emit(release)
        except Exception as error:
            self.failed.emit(str(error))


class UpdateDownloadWorker(QThread):
    progress = Signal(int)
    ready = Signal(object)
    failed = Signal(str)

    def __init__(self, service: UpdateService, release: ReleaseInfo, parent=None):
        super().__init__(parent)
        self.service = service
        self.release = release

    def run(self) -> None:
        try:
            self.ready.emit(self.service.download(self.release, self.progress.emit))
        except Exception as error:
            self.failed.emit(str(error))
