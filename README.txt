YOUTUBE LIVE CHAT OVERLAY

SETUP
1. Extract this folder.
2. Run install.bat once.
3. Run run.bat.
4. Paste the active livestream watch URL.
5. Click Start.

The installer does not require Git or a cloned repository. It creates a private
.venv folder beside the application, installs the required components there,
and verifies the app before completing. Python 3.10 or newer is the only system
prerequisite.

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
- Automatic update check at launch and every 30 seconds while running.
- New builds install automatically and restart the application when ready.
- Updates track the newest commit on the GitHub default branch.

PUBLISHING UPDATES
1. Set GITHUB_REPOSITORY to owner/repository in
   src/youtube_chat_overlay/config.py.
2. Push changes to the repository's default branch.
3. Users are notified of the newest commit and can install it with one click.

No compiled release asset is required. The updater downloads the source snapshot
provided by GitHub for the newest commit. GITHUB_REPOSITORY may also be supplied
through the YOUTUBE_OVERLAY_GITHUB_REPOSITORY environment variable.

SOURCE LAYOUT
- src/youtube_chat_overlay/ui: PySide6 windows, overlay, and presentation.
- src/youtube_chat_overlay/handlers: update and external-service handlers.
- src/youtube_chat_overlay/config.py: application identity and repository settings.

TROUBLESHOOTING
- The video must expose public live chat.
- Upcoming streams may not expose chat until they start.
- Private, members-only, age-restricted, or sign-in-only streams may not work.
- Use borderless-windowed mode for overlays over games.
