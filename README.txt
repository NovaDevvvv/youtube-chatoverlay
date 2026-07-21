YOUTUBE LIVE CHAT OVERLAY

SETUP
1. Extract this folder.
2. Run install.bat once.
3. Run run.bat.
4. Paste the active livestream watch URL.
5. Click Start.

A direct https://www.youtube.com/watch?v=... URL is the fastest and most
reliable source. Channel handles and channel URLs are also supported, but the
channel must currently be live.

FEATURES
- No YouTube Data API key.
- No pytchat dependency.
- Transparent always-on-top overlay.
- Temporary message cards with adjustable duration.
- Overlay on/off switch.
- Draggable positioning mode and screen anchors.
- Complete in-session message history.
- CSV history export.
- Automatic GitHub Release checks, downloads, and guided installation.

PUBLISHING UPDATES
1. Push this project to GitHub and create a release with a tag such as v1.1.0.
2. The included GitHub Actions workflow packages the app and attaches the ZIP
   and SHA-256 checksum expected by the updater.
3. Users are notified in the app and can install the release with one click.

The workflow injects the GitHub owner/repository and release tag into the
packaged app. For local testing, set GITHUB_REPOSITORY in
src/youtube_chat_overlay/config.py or the
YOUTUBE_OVERLAY_GITHUB_REPOSITORY environment variable.

SOURCE LAYOUT
- src/youtube_chat_overlay/ui: PySide6 windows, overlay, and presentation.
- src/youtube_chat_overlay/handlers: update and external-service handlers.
- src/youtube_chat_overlay/config.py: application identity and release settings.

TROUBLESHOOTING
- The video must expose public live chat.
- Upcoming streams may not expose chat until they start.
- Private, members-only, age-restricted, or sign-in-only streams may not work.
- Use borderless-windowed mode for overlays over games.
