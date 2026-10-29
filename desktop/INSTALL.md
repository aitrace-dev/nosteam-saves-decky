# Install NoSteam on Bazzite / Linux x64

This alpha archive includes the prebuilt desktop app and the public Python service.
It requires Python 3.11+ with venv support and an internet connection for Python
dependencies. You do not need Node.js or Decky.

Extract the archive, open a terminal in the extracted `NoSteam` directory and run:

```bash
python3 scripts/install-companion.py
```

Open NoSteam from the application menu and sign in. Use the same account on your
other devices. To open it from Gaming Mode, add the application menu entry to
Steam as a non-Steam game. Its user service is enabled for the next login; opening
the app can start the service immediately. Closing the window leaves sync running.

In Games, set up saves with Steam closed. Enable game transfers, open a configured
game and choose **Make available on my devices**. NoSteam uses its Steam installation
path and prepares the game in the background. Choose Other devices on another
machine to download. Transfers has preparation/download progress and pause/resume.
Close Steam briefly on the receiver to finish its non-Steam shortcut and exact
Proton setup. Play in Steam appears after successful registration.

Game transfers are direct LAN connections without relay fallback. Matching OS/CPU,
case-sensitive filesystems and symlink support are required. Missing privileged
host helpers require local setup. The server stores game-transfer metadata only;
cloud saves remain a separate feature. Existing saves and old runtimes are retained.
Physical controller mappings and real-game handoffs still need broader testing.
