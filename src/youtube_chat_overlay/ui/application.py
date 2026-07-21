from __future__ import annotations

import csv
import ctypes
import html
import re
import sys
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from PySide6.QtCore import QSettings, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPalette, QTextDocument, QTextOption
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import APP_NAME, APP_VERSION
from ..handlers.updates import (
    BuildInfo,
    UpdateCheckWorker,
    UpdateDownloadWorker,
    UpdateService,
)


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
WATCH_URL_RE = re.compile(
    r"https?://(?:www\.)?youtube\.com/watch\?[^\"']*?v=([A-Za-z0-9_-]{11})"
)
CANONICAL_RE = re.compile(
    r"<link[^>]+rel=[\"']canonical[\"'][^>]+href=[\"'][^\"']*?v=([A-Za-z0-9_-]{11})"
)
OG_URL_RE = re.compile(
    r"<meta[^>]+property=[\"']og:url[\"'][^>]+content=[\"'][^\"']*?v=([A-Za-z0-9_-]{11})"
)
TITLE_RE = re.compile(
    r"<meta[^>]+name=[\"']title[\"'][^>]+content=[\"']([^\"']*)[\"']"
)


@dataclass(slots=True)
class ChatMessage:
    message_id: str
    author: str
    content: str
    created_at: datetime
    author_channel_id: str | None = None
    is_owner: bool = False
    is_moderator: bool = False
    is_member: bool = False


@dataclass(slots=True)
class StreamDetails:
    video_id: str
    title: str
    url: str


def parse_item_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    text = str(value or "").strip()
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            pass

    return datetime.now(timezone.utc)


def video_id_from_source(source: str) -> str | None:
    source = source.strip()

    if VIDEO_ID_RE.fullmatch(source):
        return source

    if not source.startswith(("http://", "https://")):
        return None

    parsed = urlparse(source)
    host = (parsed.hostname or "").lower()

    if host in {"youtu.be", "www.youtu.be"}:
        candidate = parsed.path.strip("/").split("/")[0]
        return candidate if VIDEO_ID_RE.fullmatch(candidate) else None

    query_id = parse_qs(parsed.query).get("v", [None])[0]
    if query_id and VIDEO_ID_RE.fullmatch(query_id):
        return query_id

    parts = [part for part in parsed.path.split("/") if part]
    for marker in ("live", "shorts", "embed"):
        if marker in parts:
            index = parts.index(marker)
            if index + 1 < len(parts):
                candidate = parts[index + 1]
                if VIDEO_ID_RE.fullmatch(candidate):
                    return candidate

    return None


def normalise_channel_url(source: str) -> str:
    source = source.strip()

    if source.startswith(("http://", "https://")):
        parsed = urlparse(source)
        clean_path = parsed.path.rstrip("/")

        for suffix in ("/videos", "/streams", "/featured", "/about", "/live"):
            if clean_path.endswith(suffix):
                clean_path = clean_path[: -len(suffix)]
                break

        return f"https://www.youtube.com{clean_path}"

    if source.startswith("UC"):
        return f"https://www.youtube.com/channel/{source}"

    return f"https://www.youtube.com/@{source.lstrip('@')}"


def detect_channel_livestream(source: str, timeout: float = 8.0) -> StreamDetails:
    channel_url = normalise_channel_url(source)
    live_url = f"{channel_url.rstrip('/')}/live"

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    try:
        response = session.get(live_url, timeout=timeout, allow_redirects=True)
        response.raise_for_status()
    except requests.RequestException as error:
        raise RuntimeError(f"Could not open the channel's live page: {error}") from error

    page = response.text
    video_id = video_id_from_source(response.url)

    if not video_id:
        for pattern in (CANONICAL_RE, OG_URL_RE, WATCH_URL_RE):
            match = pattern.search(page)
            if match:
                video_id = match.group(1)
                break

    live_markers = (
        '"isLiveNow":true',
        '"liveStatus":"LIVE"',
        '"isLive":true',
    )

    if not video_id or not any(marker in page for marker in live_markers):
        raise RuntimeError(
            "No active livestream was detected. Paste the livestream watch URL "
            "directly for the most reliable connection."
        )

    title_match = TITLE_RE.search(page)
    title = html.unescape(title_match.group(1)) if title_match else "YouTube livestream"

    return StreamDetails(
        video_id=video_id,
        title=title,
        url=f"https://www.youtube.com/watch?v={video_id}",
    )


def _extract_json_after_marker(page: str, marker: str, start: int = 0) -> tuple[object, int] | None:
    marker_index = page.find(marker, start)
    if marker_index < 0:
        return None

    opening = page.find("{", marker_index + len(marker))
    if opening < 0:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(opening, len(page)):
        character = page[index]

        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue

        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                raw = page[opening : index + 1]
                try:
                    return json.loads(raw), index + 1
                except json.JSONDecodeError:
                    return None

    return None


def _extract_initial_data(page: str) -> dict:
    markers = (
        "var ytInitialData =",
        "window[\"ytInitialData\"] =",
        "ytInitialData =",
    )

    for marker in markers:
        result = _extract_json_after_marker(page, marker)
        if result and isinstance(result[0], dict):
            return result[0]

    raise RuntimeError(
        "YouTube did not provide the page data needed for live chat. "
        "The page may require sign-in, consent, or age verification."
    )


def _extract_ytcfg(page: str) -> dict:
    config: dict = {}
    position = 0

    while True:
        result = _extract_json_after_marker(page, "ytcfg.set(", position)
        if result is None:
            break

        value, position = result
        if isinstance(value, dict):
            config.update(value)

    api_key_match = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', page)
    version_match = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', page)

    if "INNERTUBE_API_KEY" not in config and api_key_match:
        config["INNERTUBE_API_KEY"] = api_key_match.group(1)

    if "INNERTUBE_CLIENT_VERSION" not in config and version_match:
        config["INNERTUBE_CLIENT_VERSION"] = version_match.group(1)

    return config


def _find_key(value: object, key: str) -> object | None:
    if isinstance(value, dict):
        if key in value:
            return value[key]

        for child in value.values():
            found = _find_key(child, key)
            if found is not None:
                return found

    elif isinstance(value, list):
        for child in value:
            found = _find_key(child, key)
            if found is not None:
                return found

    return None


def _runs_text(value: object) -> str:
    if not isinstance(value, dict):
        return ""

    simple_text = value.get("simpleText")
    if isinstance(simple_text, str):
        return simple_text

    output: list[str] = []
    runs = value.get("runs", [])
    if not isinstance(runs, list):
        return ""

    for run in runs:
        if not isinstance(run, dict):
            continue

        text = run.get("text")
        if isinstance(text, str):
            output.append(text)
            continue

        emoji = run.get("emoji")
        if isinstance(emoji, dict):
            shortcuts = emoji.get("shortcuts")
            if isinstance(shortcuts, list) and shortcuts:
                output.append(str(shortcuts[0]))
            else:
                output.append(str(emoji.get("emojiId", "")))

    return "".join(output)


def _continuation_details(container: object) -> tuple[str | None, int]:
    if not isinstance(container, dict):
        return None, 1000

    continuations = container.get("continuations", [])
    if not isinstance(continuations, list):
        return None, 1000

    keys = (
        "invalidationContinuationData",
        "timedContinuationData",
        "reloadContinuationData",
        "liveChatReplayContinuationData",
    )

    for continuation_wrapper in continuations:
        if not isinstance(continuation_wrapper, dict):
            continue

        for key in keys:
            data = continuation_wrapper.get(key)
            if not isinstance(data, dict):
                continue

            token = data.get("continuation")
            timeout_ms = data.get("timeoutMs", 1000)

            try:
                timeout_ms = max(250, min(10000, int(timeout_ms)))
            except (TypeError, ValueError):
                timeout_ms = 1000

            if isinstance(token, str) and token:
                return token, timeout_ms

    return None, 1000


class YouTubeLiveChatClient:
    def __init__(self, video_id: str):
        self.video_id = video_id
        self.watch_url = f"https://www.youtube.com/watch?v={video_id}"
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
                "Origin": "https://www.youtube.com",
                "Referer": self.watch_url,
            }
        )
        self.api_key = ""
        self.client_version = ""
        self.context: dict = {}
        self.continuation: str | None = None
        self.replay = False
        self.alive = False

    def connect(self) -> None:
        try:
            response = self.session.get(self.watch_url, timeout=12)
            response.raise_for_status()
        except requests.RequestException as error:
            raise RuntimeError(f"Could not open the livestream page: {error}") from error

        page = response.text
        config = _extract_ytcfg(page)
        initial_data = _extract_initial_data(page)

        self.api_key = str(config.get("INNERTUBE_API_KEY", ""))
        self.client_version = str(config.get("INNERTUBE_CLIENT_VERSION", ""))

        if not self.api_key or not self.client_version:
            raise RuntimeError(
                "YouTube did not provide its live-chat client configuration. "
                "This can happen when a consent, age, or sign-in page is shown."
            )

        context = config.get("INNERTUBE_CONTEXT")
        if isinstance(context, dict):
            self.context = context
        else:
            self.context = {
                "client": {
                    "clientName": "WEB",
                    "clientVersion": self.client_version,
                    "hl": "en",
                    "gl": "US",
                }
            }

        client = self.context.setdefault("client", {})
        if isinstance(client, dict):
            client.setdefault("clientName", "WEB")
            client.setdefault("clientVersion", self.client_version)
            client.setdefault("hl", "en")
            client.setdefault("gl", "US")

        live_chat_renderer = _find_key(initial_data, "liveChatRenderer")
        if not isinstance(live_chat_renderer, dict):
            reason = _find_key(initial_data, "reason")
            suffix = f" YouTube reports: {reason}" if isinstance(reason, str) else ""
            raise RuntimeError(
                "This video does not expose a live-chat feed. It may be upcoming, "
                "ended without replay, private, age-restricted, or have chat disabled."
                + suffix
            )

        self.replay = bool(live_chat_renderer.get("isReplay", False))
        self.continuation, _ = _continuation_details(live_chat_renderer)

        if not self.continuation:
            raise RuntimeError(
                "The livestream page was found, but YouTube did not return a live-chat continuation token."
            )

        self.alive = True

    def _parse_renderer(self, renderer_type: str, renderer: dict) -> ChatMessage | None:
        author = _runs_text(renderer.get("authorName")) or "Unknown"
        content = _runs_text(renderer.get("message"))

        if not content and renderer_type == "liveChatMembershipItemRenderer":
            content = _runs_text(renderer.get("headerSubtext")) or "New channel membership"

        if not content and renderer_type == "liveChatPaidStickerRenderer":
            sticker = renderer.get("sticker")
            if isinstance(sticker, dict):
                accessibility = sticker.get("accessibility")
                if isinstance(accessibility, dict):
                    data = accessibility.get("accessibilityData")
                    if isinstance(data, dict):
                        content = str(data.get("label", "Paid sticker"))

        purchase = _runs_text(renderer.get("purchaseAmountText"))
        if purchase:
            content = f"{content} ({purchase})" if content else purchase

        if not content:
            return None

        timestamp_usec = renderer.get("timestampUsec")
        try:
            created_at = datetime.fromtimestamp(
                int(timestamp_usec) / 1_000_000,
                tz=timezone.utc,
            )
        except (TypeError, ValueError, OSError):
            created_at = datetime.now(timezone.utc)

        is_owner = False
        is_moderator = False
        is_member = False

        badges = renderer.get("authorBadges", [])
        if isinstance(badges, list):
            for badge in badges:
                if not isinstance(badge, dict):
                    continue
                metadata = badge.get("liveChatAuthorBadgeRenderer", {})
                if not isinstance(metadata, dict):
                    continue
                style = str(metadata.get("icon", {}).get("iconType", ""))
                tooltip = str(metadata.get("tooltip", "")).casefold()
                is_owner = is_owner or style == "OWNER" or "owner" in tooltip
                is_moderator = is_moderator or style == "MODERATOR" or "moderator" in tooltip
                is_member = is_member or style in {"SPONSOR", "MEMBER"} or "member" in tooltip

        return ChatMessage(
            message_id=str(renderer.get("id", "")),
            author=author,
            content=content,
            created_at=created_at,
            author_channel_id=renderer.get("authorExternalChannelId"),
            is_owner=is_owner,
            is_moderator=is_moderator,
            is_member=is_member,
        )

    def poll(self) -> tuple[list[ChatMessage], int]:
        if not self.alive or not self.continuation:
            return [], 1000

        endpoint_name = "get_live_chat_replay" if self.replay else "get_live_chat"
        endpoint = f"https://www.youtube.com/youtubei/v1/live_chat/{endpoint_name}"
        params = {"key": self.api_key, "prettyPrint": "false"}
        payload = {
            "context": self.context,
            "continuation": self.continuation,
        }
        headers = {
            "Content-Type": "application/json",
            "X-Youtube-Client-Name": "1",
            "X-Youtube-Client-Version": self.client_version,
        }

        try:
            response = self.session.post(
                endpoint,
                params=params,
                json=payload,
                headers=headers,
                timeout=15,
            )
            response.raise_for_status()
            data = response.json()
        except requests.RequestException as error:
            detail = ""
            response_object = getattr(error, "response", None)
            if response_object is not None:
                detail = response_object.text[:300].strip()
            raise RuntimeError(
                f"YouTube rejected the live-chat request: {error}"
                + (f" — {detail}" if detail else "")
            ) from error
        except ValueError as error:
            raise RuntimeError("YouTube returned an invalid live-chat response.") from error

        continuation_contents = data.get("continuationContents", {})
        if not isinstance(continuation_contents, dict):
            error_message = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else None
            raise RuntimeError(
                str(error_message or "YouTube ended the live-chat session or did not return chat data.")
            )

        live_chat = continuation_contents.get("liveChatContinuation", {})
        if not isinstance(live_chat, dict):
            self.alive = False
            return [], 1000

        messages: list[ChatMessage] = []
        actions = live_chat.get("actions", [])

        if isinstance(actions, list):
            for action in actions:
                if not isinstance(action, dict):
                    continue

                add_action = action.get("addChatItemAction")
                if not isinstance(add_action, dict):
                    continue

                item = add_action.get("item")
                if not isinstance(item, dict) or not item:
                    continue

                renderer_type, renderer = next(iter(item.items()))
                if not isinstance(renderer, dict):
                    continue

                if renderer_type not in {
                    "liveChatTextMessageRenderer",
                    "liveChatPaidMessageRenderer",
                    "liveChatPaidStickerRenderer",
                    "liveChatMembershipItemRenderer",
                }:
                    continue

                message = self._parse_renderer(renderer_type, renderer)
                if message is not None:
                    messages.append(message)

        self.continuation, timeout_ms = _continuation_details(live_chat)
        if not self.continuation:
            self.alive = False

        return messages, timeout_ms

    def close(self) -> None:
        self.alive = False
        self.continuation = None
        self.session.close()


class LiveChatWorker(QThread):
    connected = Signal(object)
    message_received = Signal(object)
    status_changed = Signal(str)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self, source: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.source = source.strip()
        self._client: YouTubeLiveChatClient | None = None
        self._seen_ids: set[str] = set()

    def run(self) -> None:
        try:
            self.status_changed.emit("Resolving livestream…")
            direct_id = video_id_from_source(self.source)

            if direct_id:
                details = StreamDetails(
                    video_id=direct_id,
                    title="YouTube livestream",
                    url=f"https://www.youtube.com/watch?v={direct_id}",
                )
            else:
                details = detect_channel_livestream(self.source)

            if self.isInterruptionRequested():
                return

            self.status_changed.emit("Connecting to YouTube live chat…")
            self._client = YouTubeLiveChatClient(details.video_id)
            self._client.connect()

            if self.isInterruptionRequested():
                return

            self.connected.emit(details)
            self.status_changed.emit("Connected — waiting for chat messages")

            while not self.isInterruptionRequested() and self._client.alive:
                messages, timeout_ms = self._client.poll()

                for message in messages:
                    if self.isInterruptionRequested():
                        break

                    if message.message_id and message.message_id in self._seen_ids:
                        continue

                    if message.message_id:
                        self._seen_ids.add(message.message_id)

                    self.message_received.emit(message)

                remaining = max(250, timeout_ms)
                while remaining > 0 and not self.isInterruptionRequested():
                    sleep_for = min(remaining, 250)
                    self.msleep(sleep_for)
                    remaining -= sleep_for

            if not self.isInterruptionRequested():
                self.status_changed.emit("The livestream or chat has ended")

        except Exception as error:
            if not self.isInterruptionRequested():
                self.failed.emit(str(error))
        finally:
            if self._client is not None:
                self._client.close()
            self._client = None
            self.stopped.emit()

    def stop(self) -> None:
        self.requestInterruption()
        if self._client is not None:
            self._client.close()


def exact_opposite_image(source: QImage) -> QImage:
    """Return the exact per-pixel RGB opposite: (255-R, 255-G, 255-B)."""
    opposite = source.convertToFormat(QImage.Format.Format_ARGB32)
    opposite.invertPixels(QImage.InvertMode.InvertRgb)
    return opposite


class AdaptiveContrastLabel(QLabel):
    def __init__(
        self,
        text: str,
        font_size: int,
        bold: bool = False,
        word_wrap: bool = False,
        parent: QWidget | None = None,
    ):
        super().__init__(text, parent)
        self.setWordWrap(word_wrap)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)

        font = QFont("Segoe UI")
        font.setPixelSize(font_size)
        font.setWeight(QFont.Weight.Bold if bold else QFont.Weight.Normal)
        self.setFont(font)

        self._text_mask = QImage()
        self._filtered_text = QImage()
        self._mask_dirty = True

    def setText(self, text: str) -> None:
        super().setText(text)
        self._mask_dirty = True
        self.updateGeometry()
        self.update()

    def resizeEvent(self, event) -> None:
        self._mask_dirty = True
        super().resizeEvent(event)

    def _ensure_text_mask(self) -> None:
        if (
            not self._mask_dirty
            and not self._text_mask.isNull()
            and self._text_mask.size() == self.size()
        ):
            return

        width = max(1, self.width())
        height = max(1, self.height())
        mask = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        mask.fill(Qt.GlobalColor.transparent)

        document = QTextDocument()
        document.setDocumentMargin(0)
        document.setDefaultFont(self.font())
        document.setPlainText(self.text())
        document.setTextWidth(width)

        option = document.defaultTextOption()
        option.setWrapMode(
            QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere
            if self.wordWrap()
            else QTextOption.WrapMode.NoWrap
        )
        document.setDefaultTextOption(option)

        painter = QPainter(mask)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        document.drawContents(painter)
        painter.end()

        self._text_mask = mask
        self._mask_dirty = False

    def update_text_contrast(self) -> None:
        if not self.isVisible() or self.width() <= 0 or self.height() <= 0:
            return

        self._ensure_text_mask()

        centre = self.mapToGlobal(self.rect().center())
        screen = QApplication.screenAt(centre) or QApplication.primaryScreen()
        if screen is None:
            return

        top_left = self.mapToGlobal(self.rect().topLeft())
        pixmap = screen.grabWindow(
            0,
            top_left.x(),
            top_left.y(),
            max(1, self.width()),
            max(1, self.height()),
        )
        background = pixmap.toImage()
        if background.isNull():
            return

        # QScreen can return physical pixels on a scaled display. Convert the
        # capture to this widget's logical pixel size before applying the mask.
        if background.size() != self.size():
            background = background.scaled(
                self.size(),
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )

        # Exact digital complementary colour for every captured pixel:
        # black -> white, white -> black, red -> cyan, blue -> yellow, etc.
        background = exact_opposite_image(background)

        filtered = QImage(
            max(1, self.width()),
            max(1, self.height()),
            QImage.Format.Format_ARGB32_Premultiplied,
        )
        filtered.fill(Qt.GlobalColor.transparent)

        painter = QPainter(filtered)
        painter.drawImage(0, 0, background)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
        painter.drawImage(0, 0, self._text_mask)
        painter.end()

        self._filtered_text = filtered
        self.update()

    def paintEvent(self, event) -> None:
        self._ensure_text_mask()

        painter = QPainter(self)
        if not self._filtered_text.isNull():
            painter.drawImage(0, 0, self._filtered_text)
        else:
            fallback = QImage(
                max(1, self.width()),
                max(1, self.height()),
                QImage.Format.Format_ARGB32_Premultiplied,
            )
            fallback.fill(Qt.GlobalColor.white)
            fallback_painter = QPainter(fallback)
            fallback_painter.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_DestinationIn
            )
            fallback_painter.drawImage(0, 0, self._text_mask)
            fallback_painter.end()
            painter.drawImage(0, 0, fallback)
        painter.end()


class MessageCard(QWidget):
    expired = Signal(object)

    def __init__(self, message: ChatMessage, duration_seconds: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.message = message

        role = ""
        if message.is_owner:
            role = " • OWNER"
        elif message.is_moderator:
            role = " • MOD"
        elif message.is_member:
            role = " • MEMBER"

        self.author_label = AdaptiveContrastLabel(
            f"{message.author}{role}",
            font_size=13,
            bold=True,
            parent=self,
        )
        self.content_label = AdaptiveContrastLabel(
            message.content,
            font_size=16,
            word_wrap=True,
            parent=self,
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(4)
        layout.addWidget(self.author_label)
        layout.addWidget(self.content_label)

        self.setObjectName("messageCard")
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAutoFillBackground(False)
        self.setStyleSheet(
            """
            QWidget#messageCard {
                background: transparent;
                border: none;
            }
            """
        )

        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(lambda: self.expired.emit(self))
        self.timer.start(max(1, duration_seconds) * 1000)

    def update_text_contrast(self) -> None:
        self.author_label.update_text_contrast()
        self.content_label.update_text_contrast()


class OverlayWindow(QWidget):
    position_changed = Signal(int, int)
    size_changed = Signal()

    def __init__(self):
        super().__init__()
        self.cards: list[MessageCard] = []
        self.overlay_enabled = True
        self.edit_mode = False
        self._drag_offset = None
        self._overlay_width = 520

        self.setWindowTitle("YouTube Chat Overlay")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

        self._exclude_from_screen_capture()

        self.drag_handle = QLabel("DRAG OVERLAY — press Position Overlay again when done")
        self.drag_handle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drag_handle.setStyleSheet(
            """
            QLabel {
                color: white;
                background: rgba(35, 134, 54, 235);
                border: 2px dashed white;
                border-radius: 10px;
                padding: 12px;
                font-weight: 700;
            }
            """
        )
        self.drag_handle.hide()

        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(8)
        self.layout.addWidget(self.drag_handle)

        self.setFixedWidth(self._overlay_width)

        self.contrast_timer = QTimer(self)
        self.contrast_timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.contrast_timer.setInterval(round(1000 / 30))
        self.contrast_timer.timeout.connect(self._update_card_colours)
        self.contrast_timer.start()

        self.hide()

    def _exclude_from_screen_capture(self) -> None:
        if sys.platform != "win32":
            return

        # Prevent QScreen.grabWindow() from capturing this overlay itself, so
        # every glyph pixel is calculated from the real window underneath it.
        WDA_EXCLUDEFROMCAPTURE = 0x00000011
        try:
            hwnd = int(self.winId())
            ctypes.windll.user32.SetWindowDisplayAffinity(
                ctypes.c_void_p(hwnd),
                WDA_EXCLUDEFROMCAPTURE,
            )
        except (AttributeError, OSError, TypeError, ValueError):
            pass

    def _update_card_colours(self) -> None:
        if not self.isVisible() or self.edit_mode:
            return

        for card in self.cards:
            card.update_text_contrast()

    def set_overlay_enabled(self, enabled: bool) -> None:
        self.overlay_enabled = enabled
        self._update_visibility()

    def set_overlay_width(self, width: int) -> None:
        self._overlay_width = max(320, min(1000, width))
        self.setFixedWidth(self._overlay_width)
        self._refresh_size()

    def set_edit_mode(self, enabled: bool) -> None:
        self.edit_mode = enabled
        self.drag_handle.setVisible(enabled)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, not enabled)
        self._refresh_size()
        self._update_visibility()

        if enabled:
            self.raise_()
            self.activateWindow()

    def show_message(self, message: ChatMessage, duration_seconds: int) -> None:
        card = MessageCard(message, duration_seconds, self)
        card.expired.connect(self.remove_card)
        self.cards.insert(0, card)
        self.layout.insertWidget(1, card)

        self._refresh_size()
        self._update_visibility()
        self.raise_()
        QTimer.singleShot(0, card.update_text_contrast)

    def remove_card(self, card: MessageCard) -> None:
        if card not in self.cards:
            return

        self.cards.remove(card)
        self.layout.removeWidget(card)
        card.deleteLater()
        self._refresh_size()
        self._update_visibility()

    def clear_messages(self) -> None:
        for card in list(self.cards):
            self.remove_card(card)

    def _refresh_size(self) -> None:
        QTimer.singleShot(0, self._finish_refresh_size)

    def _finish_refresh_size(self) -> None:
        self.adjustSize()
        self.setFixedWidth(self._overlay_width)
        self.size_changed.emit()

    def _update_visibility(self) -> None:
        should_show = self.edit_mode or (self.overlay_enabled and bool(self.cards))
        self.setVisible(should_show)

    def mousePressEvent(self, event) -> None:
        if self.edit_mode and event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self.edit_mode and self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self.edit_mode and event.button() == Qt.MouseButton.LeftButton:
            self._drag_offset = None
            self.position_changed.emit(self.x(), self.y())
            event.accept()
            return
        super().mouseReleaseEvent(event)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("Nova", "YouTubeChatOverlay")
        self.worker: LiveChatWorker | None = None
        self.history: list[ChatMessage] = []
        self._updating_position = False
        self.update_service = UpdateService(Path(__file__).resolve().parents[3])
        self.update_worker: UpdateCheckWorker | UpdateDownloadWorker | None = None
        self.pending_build: BuildInfo | None = None
        self.downloaded_update: tuple[Path, str] | None = None

        self.overlay = OverlayWindow()
        self.overlay.position_changed.connect(self._overlay_was_dragged)
        self.overlay.size_changed.connect(self._overlay_size_changed)

        self.setWindowTitle(f"{APP_NAME} · v{APP_VERSION}")
        self.resize(980, 680)
        self.setMinimumSize(780, 560)

        root = QWidget()
        self.setCentralWidget(root)
        main_layout = QVBoxLayout(root)
        main_layout.setContentsMargins(18, 18, 18, 18)
        main_layout.setSpacing(14)

        header = QHBoxLayout()
        heading = QVBoxLayout()
        title = QLabel(APP_NAME)
        title.setObjectName("title")
        subtitle = QLabel(
            "A clean, configurable live-chat overlay for streaming and recording."
        )
        subtitle.setObjectName("subtitle")
        subtitle.setWordWrap(True)
        heading.addWidget(title)
        heading.addWidget(subtitle)
        header.addLayout(heading, 1)
        self.version_label = QLabel(f"VERSION {APP_VERSION}")
        self.version_label.setObjectName("versionBadge")
        header.addWidget(self.version_label, 0, Qt.AlignmentFlag.AlignTop)
        main_layout.addLayout(header)

        self.update_panel = QWidget()
        self.update_panel.setObjectName("updatePanel")
        update_layout = QHBoxLayout(self.update_panel)
        update_layout.setContentsMargins(14, 10, 14, 10)
        self.update_status = QLabel("Updates are checked automatically at startup.")
        self.update_status.setObjectName("updateStatus")
        self.update_status.setWordWrap(True)
        self.update_progress = QProgressBar()
        self.update_progress.setRange(0, 100)
        self.update_progress.setFixedWidth(150)
        self.update_progress.hide()
        self.update_button = QPushButton("Check for updates")
        self.update_button.setObjectName("secondaryButton")
        self.update_button.clicked.connect(lambda: self.check_for_updates(manual=True))
        update_layout.addWidget(self.update_status, 1)
        update_layout.addWidget(self.update_progress)
        update_layout.addWidget(self.update_button)
        main_layout.addWidget(self.update_panel)

        connection_group = QGroupBox("Connection")
        connection_layout = QGridLayout(connection_group)

        self.source_input = QLineEdit()
        self.source_input.setPlaceholderText(
            "Livestream URL, video ID, @channel, or YouTube channel URL"
        )
        self.source_input.returnPressed.connect(self.start_chat)

        self.start_button = QPushButton("Start")
        self.start_button.clicked.connect(self.start_chat)

        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop_chat)

        self.status_label = QLabel("Stopped")
        self.status_label.setObjectName("status")

        connection_layout.addWidget(QLabel("Source"), 0, 0)
        connection_layout.addWidget(self.source_input, 0, 1)
        connection_layout.addWidget(self.start_button, 0, 2)
        connection_layout.addWidget(self.stop_button, 0, 3)
        connection_layout.addWidget(self.status_label, 1, 1, 1, 3)
        main_layout.addWidget(connection_group)

        overlay_group = QGroupBox("Overlay")
        overlay_layout = QGridLayout(overlay_group)

        self.overlay_enabled = QCheckBox("Overlay enabled")
        self.overlay_enabled.setChecked(True)
        self.overlay_enabled.toggled.connect(self.overlay.set_overlay_enabled)
        self.overlay_enabled.toggled.connect(self.save_settings)

        self.position_button = QPushButton("Position overlay")
        self.position_button.setCheckable(True)
        self.position_button.toggled.connect(self.toggle_position_mode)

        self.preview_button = QPushButton("Preview")
        self.preview_button.clicked.connect(self.preview_overlay)

        self.duration_spin = QSpinBox()
        self.duration_spin.setRange(1, 60)
        self.duration_spin.setSuffix(" seconds")
        self.duration_spin.setValue(10)
        self.duration_spin.valueChanged.connect(self.save_settings)

        self.width_spin = QSpinBox()
        self.width_spin.setRange(320, 1000)
        self.width_spin.setSingleStep(20)
        self.width_spin.setSuffix(" px")
        self.width_spin.setValue(520)
        self.width_spin.valueChanged.connect(self.overlay.set_overlay_width)
        self.width_spin.valueChanged.connect(self.save_settings)

        self.screen_combo = QComboBox()
        self._populate_screens()
        self.screen_combo.currentIndexChanged.connect(self.apply_anchor_position)
        self.screen_combo.currentIndexChanged.connect(self.save_settings)

        self.anchor_combo = QComboBox()
        self.anchor_combo.addItems(
            [
                "Custom",
                "Top left",
                "Top centre",
                "Top right",
                "Centre left",
                "Centre",
                "Centre right",
                "Bottom left",
                "Bottom centre",
                "Bottom right",
            ]
        )
        self.anchor_combo.currentTextChanged.connect(self.apply_anchor_position)
        self.anchor_combo.currentTextChanged.connect(self.save_settings)

        self.x_spin = QSpinBox()
        self.x_spin.setRange(-10000, 10000)
        self.x_spin.valueChanged.connect(self._position_spin_changed)

        self.y_spin = QSpinBox()
        self.y_spin.setRange(-10000, 10000)
        self.y_spin.valueChanged.connect(self._position_spin_changed)

        overlay_layout.addWidget(self.overlay_enabled, 0, 0)
        overlay_layout.addWidget(self.position_button, 0, 1)
        overlay_layout.addWidget(self.preview_button, 0, 2)
        overlay_layout.addWidget(QLabel("Visible for"), 1, 0)
        overlay_layout.addWidget(self.duration_spin, 1, 1)
        overlay_layout.addWidget(QLabel("Width"), 1, 2)
        overlay_layout.addWidget(self.width_spin, 1, 3)
        overlay_layout.addWidget(QLabel("Screen"), 2, 0)
        overlay_layout.addWidget(self.screen_combo, 2, 1)
        overlay_layout.addWidget(QLabel("Anchor"), 2, 2)
        overlay_layout.addWidget(self.anchor_combo, 2, 3)
        overlay_layout.addWidget(QLabel("X"), 3, 0)
        overlay_layout.addWidget(self.x_spin, 3, 1)
        overlay_layout.addWidget(QLabel("Y"), 3, 2)
        overlay_layout.addWidget(self.y_spin, 3, 3)
        main_layout.addWidget(overlay_group)

        history_header = QHBoxLayout()
        history_title = QLabel("Message history")
        history_title.setObjectName("sectionTitle")

        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clear_history)

        self.export_button = QPushButton("Export CSV")
        self.export_button.clicked.connect(self.export_history)

        history_header.addWidget(history_title)
        history_header.addStretch()
        history_header.addWidget(self.clear_button)
        history_header.addWidget(self.export_button)
        main_layout.addLayout(history_header)

        self.history_table = QTableWidget(0, 3)
        self.history_table.setHorizontalHeaderLabels(["Time", "User", "Message"])
        self.history_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.history_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.history_table.setAlternatingRowColors(True)
        self.history_table.verticalHeader().setVisible(False)
        self.history_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.history_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        main_layout.addWidget(self.history_table, 1)

        self._apply_styles()
        self.load_settings()
        self.update_timer = QTimer(self)
        self.update_timer.setInterval(30_000)
        self.update_timer.timeout.connect(self.check_for_updates)
        if self.settings.value("autoUpdate", True, type=bool):
            QTimer.singleShot(1200, self.check_for_updates)
            self.update_timer.start()

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background: #081317;
                color: #f0f6fc;
                font-family: "Segoe UI";
                font-size: 13px;
            }
            QLabel#title {
                font-size: 26px;
                font-weight: 800;
                color: white;
            }
            QLabel#subtitle {
                color: #9da7b0;
                font-size: 13px;
            }
            QLabel#versionBadge {
                background: #15382a;
                color: #70e1a1;
                border: 1px solid #256b49;
                border-radius: 10px;
                padding: 5px 9px;
                font-size: 10px;
                font-weight: 800;
            }
            QWidget#updatePanel {
                background: #0d1d22;
                border: 1px solid #233b44;
                border-radius: 10px;
            }
            QLabel#updateStatus { color: #b8c7ce; background: transparent; }
            QLabel#sectionTitle {
                font-size: 17px;
                font-weight: 700;
            }
            QLabel#status {
                color: #7ee787;
            }
            QGroupBox {
                border: 1px solid #23343b;
                border-radius: 12px;
                margin-top: 12px;
                padding: 14px;
                font-weight: 700;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 6px;
            }
            QLineEdit, QSpinBox, QComboBox, QTableWidget {
                background: #0d1d22;
                border: 1px solid #2b3f47;
                border-radius: 8px;
                padding: 7px;
                selection-background-color: #238636;
            }
            QTableWidget {
                gridline-color: #1c2d34;
                alternate-background-color: #0b181d;
            }
            QHeaderView::section {
                background: #102229;
                color: #c9d1d9;
                border: none;
                border-bottom: 1px solid #2b3f47;
                padding: 8px;
                font-weight: 700;
            }
            QPushButton {
                background: #1f6f32;
                border: 1px solid #2ea043;
                border-radius: 8px;
                padding: 8px 14px;
                font-weight: 700;
            }
            QPushButton:hover {
                background: #2b8641;
            }
            QPushButton:pressed, QPushButton:checked {
                background: #145523;
            }
            QPushButton:disabled {
                background: #1b2a2f;
                border-color: #2b3f47;
                color: #66757d;
            }
            QPushButton#secondaryButton {
                background: #13272e;
                border-color: #34515c;
            }
            QPushButton#secondaryButton:hover { background: #1a3540; }
            QProgressBar {
                background: #081317;
                border: 1px solid #2b3f47;
                border-radius: 5px;
                height: 9px;
                text-align: center;
                color: transparent;
            }
            QProgressBar::chunk { background: #35b66f; border-radius: 4px; }
            QCheckBox {
                spacing: 8px;
                font-weight: 700;
            }
            """
        )

    def _populate_screens(self) -> None:
        self.screen_combo.clear()
        for index, screen in enumerate(QApplication.screens(), start=1):
            geometry = screen.geometry()
            self.screen_combo.addItem(
                f"{index}: {screen.name()} ({geometry.width()}×{geometry.height()})"
            )

    def check_for_updates(self, manual: bool = False) -> None:
        if self.update_worker and self.update_worker.isRunning():
            return
        self.update_button.setEnabled(False)
        self.update_status.setText("Checking the latest GitHub build…")
        worker = UpdateCheckWorker(self.update_service, self)
        self.update_worker = worker
        worker.found.connect(self._update_available)
        worker.current.connect(lambda: self._update_check_current(manual))
        worker.failed.connect(lambda error: self._update_check_failed(error, manual))
        worker.finished.connect(lambda current=worker: self._update_worker_finished(current))
        worker.start()

    def _update_available(self, build: BuildInfo) -> None:
        self.pending_build = build
        self.update_status.setText(f"Build {build.version} is ready — {build.title}")
        self.update_button.setText("Install update")
        self.update_button.setEnabled(True)
        try:
            self.update_button.clicked.disconnect()
        except RuntimeError:
            pass
        self.update_button.clicked.connect(self.install_update)
        self.update_status.setText(
            f"Build {build.version} found — installing automatically…"
        )
        QTimer.singleShot(100, self.install_update)

    def _update_check_current(self, manual: bool) -> None:
        self.update_status.setText(f"You’re up to date · version {APP_VERSION}")
        if manual:
            QMessageBox.information(self, "No updates", "You already have the latest version.")

    def _update_check_failed(self, error: str, manual: bool) -> None:
        self.update_status.setText("Automatic update check unavailable")
        if manual:
            QMessageBox.warning(self, "Update check failed", error)

    def _update_worker_finished(self, worker) -> None:
        if self.pending_build is None:
            self.update_button.setEnabled(True)
        worker.deleteLater()
        if self.update_worker is worker:
            self.update_worker = None
        if isinstance(worker, UpdateDownloadWorker) and self.downloaded_update:
            staged_dir, revision = self.downloaded_update
            self.downloaded_update = None
            self._begin_update_restart(staged_dir, revision)

    def install_update(self) -> None:
        build = self.pending_build
        if build is None:
            return
        if isinstance(self.update_worker, UpdateCheckWorker) and self.update_worker.isRunning():
            QTimer.singleShot(100, self.install_update)
            return
        if isinstance(self.update_worker, UpdateDownloadWorker) and self.update_worker.isRunning():
            return
        self.update_button.setEnabled(False)
        self.update_progress.setValue(0)
        self.update_progress.show()
        self.update_status.setText(f"Downloading build {build.version}…")
        worker = UpdateDownloadWorker(self.update_service, build, self)
        self.update_worker = worker
        worker.progress.connect(self.update_progress.setValue)
        worker.ready.connect(self._update_downloaded)
        worker.failed.connect(self._update_install_failed)
        worker.finished.connect(lambda current=worker: self._update_worker_finished(current))
        worker.start()

    def _update_downloaded(self, staged_dir: Path, revision: str) -> None:
        self.downloaded_update = (staged_dir, revision)
        self.update_progress.setValue(100)
        self.update_status.setText("Update downloaded. Preparing restart…")

    def _begin_update_restart(self, staged_dir: Path, revision: str) -> None:
        try:
            self.update_service.launch_installer(staged_dir, revision)
        except Exception as error:
            self._update_install_failed(str(error))
            return
        self.update_status.setText("Update ready. Restarting…")
        QTimer.singleShot(250, QApplication.quit)

    def _update_install_failed(self, error: str) -> None:
        self.update_progress.hide()
        self.update_button.setEnabled(True)
        self.update_status.setText("Update installation failed")
        QMessageBox.critical(self, "Update failed", error)

    def load_settings(self) -> None:
        self.source_input.setText(self.settings.value("source", ""))
        self.overlay_enabled.setChecked(
            self.settings.value("overlayEnabled", True, type=bool)
        )
        self.duration_spin.setValue(
            self.settings.value("duration", 10, type=int)
        )
        self.width_spin.setValue(
            self.settings.value("width", 520, type=int)
        )

        screen_index = self.settings.value("screen", 0, type=int)
        self.screen_combo.setCurrentIndex(
            min(max(0, screen_index), max(0, self.screen_combo.count() - 1))
        )

        anchor = self.settings.value("anchor", "Top right")
        anchor_index = self.anchor_combo.findText(anchor)
        self.anchor_combo.setCurrentIndex(anchor_index if anchor_index >= 0 else 3)

        self._updating_position = True
        self.x_spin.setValue(self.settings.value("x", 50, type=int))
        self.y_spin.setValue(self.settings.value("y", 50, type=int))
        self._updating_position = False

        self.overlay.set_overlay_enabled(self.overlay_enabled.isChecked())
        self.overlay.set_overlay_width(self.width_spin.value())
        QTimer.singleShot(0, self.apply_anchor_position)

    def save_settings(self, *_args) -> None:
        self.settings.setValue("source", self.source_input.text().strip())
        self.settings.setValue("overlayEnabled", self.overlay_enabled.isChecked())
        self.settings.setValue("duration", self.duration_spin.value())
        self.settings.setValue("width", self.width_spin.value())
        self.settings.setValue("screen", self.screen_combo.currentIndex())
        self.settings.setValue("anchor", self.anchor_combo.currentText())
        self.settings.setValue("x", self.x_spin.value())
        self.settings.setValue("y", self.y_spin.value())

    def start_chat(self) -> None:
        source = self.source_input.text().strip()
        if not source:
            QMessageBox.warning(self, "Missing source", "Enter a livestream URL or channel first.")
            return

        self.stop_chat(wait=True)
        self.save_settings()

        worker = LiveChatWorker(source, self)
        self.worker = worker
        worker.connected.connect(self._chat_connected)
        worker.message_received.connect(self._message_received)
        worker.status_changed.connect(self.status_label.setText)
        worker.failed.connect(self._chat_failed)
        worker.stopped.connect(lambda current=worker: self._worker_stopped(current))

        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.source_input.setEnabled(False)
        worker.start()

    def stop_chat(self, wait: bool = False) -> None:
        if self.worker is None:
            return

        self.status_label.setText("Stopping…")
        self.worker.stop()

        if wait:
            self.worker.wait(2000)

    def _chat_connected(self, details: StreamDetails) -> None:
        self.status_label.setText(f"Connected: {details.title}")

    def _chat_failed(self, error: str) -> None:
        self.status_label.setText("Connection failed")
        QMessageBox.critical(self, "YouTube chat error", error)

    def _worker_stopped(self, worker: LiveChatWorker) -> None:
        worker.deleteLater()

        if self.worker is not worker:
            return

        self.worker = None
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.source_input.setEnabled(True)
        if self.status_label.text() == "Stopping…":
            self.status_label.setText("Stopped")

    def _message_received(self, message: ChatMessage) -> None:
        self.history.append(message)

        row = 0
        self.history_table.insertRow(row)
        local_time = message.created_at.astimezone().strftime("%H:%M:%S")
        self.history_table.setItem(row, 0, QTableWidgetItem(local_time))
        self.history_table.setItem(row, 1, QTableWidgetItem(message.author))
        self.history_table.setItem(row, 2, QTableWidgetItem(message.content))

        if self.overlay_enabled.isChecked():
            self.overlay.show_message(message, self.duration_spin.value())
            self.apply_anchor_position()

    def preview_overlay(self) -> None:
        preview = ChatMessage(
            message_id="preview",
            author="Preview User",
            content="This is how a live-chat message will appear on your screen.",
            created_at=datetime.now(timezone.utc),
            is_member=True,
        )
        self.overlay.show_message(preview, self.duration_spin.value())
        self.apply_anchor_position()

    def clear_history(self) -> None:
        self.history.clear()
        self.history_table.setRowCount(0)
        self.overlay.clear_messages()

    def export_history(self) -> None:
        if not self.history:
            QMessageBox.information(self, "Nothing to export", "No chat messages have been received yet.")
            return

        default_name = Path.home() / "youtube_chat_history.csv"
        file_name, _ = QFileDialog.getSaveFileName(
            self,
            "Export chat history",
            str(default_name),
            "CSV files (*.csv)",
        )
        if not file_name:
            return

        try:
            with open(file_name, "w", newline="", encoding="utf-8-sig") as file:
                writer = csv.writer(file)
                writer.writerow(["time", "author", "message", "channel_id"])
                for message in self.history:
                    writer.writerow(
                        [
                            message.created_at.isoformat(),
                            message.author,
                            message.content,
                            message.author_channel_id or "",
                        ]
                    )
        except OSError as error:
            QMessageBox.critical(self, "Export failed", str(error))
            return

        QMessageBox.information(self, "Export complete", f"Saved to:\n{file_name}")

    def toggle_position_mode(self, enabled: bool) -> None:
        self.overlay.set_edit_mode(enabled)
        self.position_button.setText("Finish positioning" if enabled else "Position overlay")

        if enabled:
            self.anchor_combo.setCurrentText("Custom")
            self.overlay.move(self.x_spin.value(), self.y_spin.value())

    def _position_spin_changed(self) -> None:
        if self._updating_position:
            return

        self.anchor_combo.setCurrentText("Custom")
        self.overlay.move(self.x_spin.value(), self.y_spin.value())
        self.save_settings()

    def _overlay_was_dragged(self, x: int, y: int) -> None:
        self._updating_position = True
        self.x_spin.setValue(x)
        self.y_spin.setValue(y)
        self._updating_position = False
        self.anchor_combo.setCurrentText("Custom")
        self.save_settings()

    def _overlay_size_changed(self) -> None:
        if self.anchor_combo.currentText() != "Custom":
            self.apply_anchor_position()

    def apply_anchor_position(self, *_args) -> None:
        if self.screen_combo.count() == 0:
            return

        screens = QApplication.screens()
        screen_index = min(self.screen_combo.currentIndex(), len(screens) - 1)
        geometry = screens[screen_index].availableGeometry()
        anchor = self.anchor_combo.currentText()
        margin = 24

        if anchor == "Custom":
            x = self.x_spin.value()
            y = self.y_spin.value()
        else:
            overlay_width = self.overlay.width()
            overlay_height = self.overlay.sizeHint().height()

            if "left" in anchor.lower():
                x = geometry.left() + margin
            elif "right" in anchor.lower():
                x = geometry.right() - overlay_width - margin + 1
            else:
                x = geometry.left() + (geometry.width() - overlay_width) // 2

            if anchor.lower().startswith("top"):
                y = geometry.top() + margin
            elif anchor.lower().startswith("bottom"):
                y = geometry.bottom() - overlay_height - margin + 1
            else:
                y = geometry.top() + (geometry.height() - overlay_height) // 2

            self._updating_position = True
            self.x_spin.setValue(x)
            self.y_spin.setValue(y)
            self._updating_position = False

        self.overlay.move(x, y)
        self.save_settings()

    def closeEvent(self, event) -> None:
        self.save_settings()
        self.stop_chat(wait=True)
        self.overlay.close()
        event.accept()


def configure_palette(app: QApplication) -> None:
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor("#081317"))
    palette.setColor(QPalette.ColorRole.WindowText, QColor("#f0f6fc"))
    palette.setColor(QPalette.ColorRole.Base, QColor("#0d1d22"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#0b181d"))
    palette.setColor(QPalette.ColorRole.Text, QColor("#f0f6fc"))
    palette.setColor(QPalette.ColorRole.Button, QColor("#1f6f32"))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor("#ffffff"))
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#238636"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    app.setPalette(palette)


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("YouTube Live Chat Overlay")
    app.setOrganizationName("Nova")
    app.setStyle("Fusion")
    configure_palette(app)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
