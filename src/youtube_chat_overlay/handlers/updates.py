"""Latest-source update discovery, download, and Windows installation."""

from __future__ import annotations

import os
import re
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
)


@dataclass(slots=True)
class BuildInfo:
    revision: str
    version: str
    title: str
    notes: str
    page_url: str
    download_url: str


def discover_repository(app_dir: Path) -> str:
    configured = (os.getenv("YOUTUBE_OVERLAY_GITHUB_REPOSITORY") or GITHUB_REPOSITORY).strip()
    if configured:
        configured = re.sub(r"^https?://github\.com/", "", configured, flags=re.IGNORECASE)
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

    def installed_revision(self) -> str:
        marker = self.app_dir / ".update-revision"
        if marker.is_file():
            return marker.read_text(encoding="utf-8", errors="ignore").strip()

        head = self.app_dir / ".git" / "HEAD"
        if head.is_file():
            value = head.read_text(encoding="utf-8", errors="ignore").strip()
            if value.startswith("ref: "):
                ref = self.app_dir / ".git" / value[5:]
                if ref.is_file():
                    return ref.read_text(encoding="utf-8", errors="ignore").strip()
            elif re.fullmatch(r"[a-fA-F0-9]{40}", value):
                return value
        return ""

    def latest_build(self) -> BuildInfo | None:
        if not self.repository:
            raise UpdateError(
                "No GitHub repository is configured. Set GITHUB_REPOSITORY in config.py "
                "to owner/repository before distributing the app."
            )
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise UpdateError(
                f'GitHub repository "{self.repository}" is incomplete. Use owner/repository '
                '(for example "octocat/youtube-chatoverlay"), or paste the full GitHub URL.'
            )
        response = self.session.get(f"{GITHUB_API_ROOT}/repos/{self.repository}", timeout=12)
        if response.status_code == 404:
            raise UpdateError("The configured GitHub repository was not found or is private.")
        response.raise_for_status()
        repository = response.json()
        branch = str(repository.get("default_branch") or "main")
        response = self.session.get(
            f"{GITHUB_API_ROOT}/repos/{self.repository}/commits/{branch}", timeout=12
        )
        response.raise_for_status()
        data = response.json()
        revision = str(data.get("sha") or "")
        if not revision:
            raise UpdateError("GitHub did not return a revision for the default branch.")
        if self.installed_revision().lower() == revision.lower():
            return None
        commit = data.get("commit") or {}
        message = str(commit.get("message") or "Latest source update")
        return BuildInfo(
            revision=revision,
            version=revision[:7],
            title=message.splitlines()[0],
            notes=message,
            page_url=str(data.get("html_url") or ""),
            download_url=f"{GITHUB_API_ROOT}/repos/{self.repository}/zipball/{revision}",
        )

    def download(self, build: BuildInfo, progress) -> Path:
        target_dir = Path(tempfile.mkdtemp(prefix="youtube-overlay-update-"))
        archive = target_dir / "source-update.zip"
        with self.session.get(build.download_url, stream=True, timeout=30) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length", 0))
            received = 0
            with archive.open("wb") as output:
                for chunk in response.iter_content(1024 * 256):
                    if chunk:
                        output.write(chunk)
                        received += len(chunk)
                        progress(int(received * 100 / total) if total else 0)

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

    def launch_installer(self, staged_dir: Path, revision: str) -> None:
        script = staged_dir.parent / "install-update.ps1"
        app_dir = str(self.app_dir).replace("'", "''")
        source = str(staged_dir).replace("'", "''")
        executable = str(Path(sys.executable)).replace("'", "''")
        source_root = str(self.app_dir / "src").replace("'", "''")
        safe_revision = re.sub(r"[^a-fA-F0-9]", "", revision)
        script.write_text(
            "$ErrorActionPreference = 'Stop'\n"
            f"Wait-Process -Id {os.getpid()} -ErrorAction SilentlyContinue\n"
            f"Copy-Item -Path '{source}\\*' -Destination '{app_dir}' -Recurse -Force\n"
            f"Set-Content -Path '{app_dir}\\.update-revision' -Value '{safe_revision}' -Encoding ascii\n"
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
            build = self.service.latest_build()
            self.current.emit() if build is None else self.found.emit(build)
        except Exception as error:
            self.failed.emit(str(error))


class UpdateDownloadWorker(QThread):
    progress = Signal(int)
    ready = Signal(object, str)
    failed = Signal(str)

    def __init__(self, service: UpdateService, build: BuildInfo, parent=None):
        super().__init__(parent)
        self.service = service
        self.build = build

    def run(self) -> None:
        try:
            staged = self.service.download(self.build, self.progress.emit)
            self.ready.emit(staged, self.build.revision)
        except Exception as error:
            self.failed.emit(str(error))
