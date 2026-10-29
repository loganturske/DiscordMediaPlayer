# Run locally with Docker Compose

Docker bundles Python, Node.js, FFmpeg, libopus, and the bot's Python dependencies. You only need Docker and a Discord bot token on your computer; a separate Python or Node.js installation is not required.

Use Docker Desktop on macOS or Windows, or Docker Engine with the Compose plugin on Linux. On Windows, select Linux containers. Start Docker, then confirm the commands are available:

```sh
docker version
docker compose version
```

Your computer and Docker must stay running for the bot to stay online. Sleep, shutdown, or loss of Internet access interrupts playback. The container needs outbound HTTPS/WebSocket and Discord voice UDP traffic; no incoming port needs to be opened or published. A VPN or firewall can still prevent voice connections.

## Configure and start

Download or clone this repository and open a terminal in its top-level directory, beside `compose.yaml` and `Dockerfile`.

Create your private configuration file:

**macOS or Linux**

```sh
cp .env.example .env
chmod 600 .env
```

**Windows PowerShell**

```powershell
Copy-Item .env.example .env
notepad .env
```

Edit `.env` in a local text editor. Set `DISCORD_TOKEN` to your bot token from the [Discord Developer Portal](https://discord.com/developers/applications). Set `ALLOWED_GUILD_IDS` to the comma-separated IDs of servers you trust, especially when using YouTube cookies. `DISCORD_GUILD_ID` is optional: set it for immediate registration in one development server, or leave it blank for global commands.

Invite the bot with the `bot` and `applications.commands` scopes and **View Channels, Send Messages, Connect, Speak** permissions, as described in the [README](../README.md). Keep `SYNC_COMMANDS=true` on the first run and after command definitions change.

The Compose setup reads `.env`; direct `python -m musicbot` execution does not. Keep `.env` outside source control and do not put your token in the Dockerfile, image build arguments, or shell history. Git and Docker ignore this file. Anyone with control of your Docker daemon can inspect container credentials.

Build and start:

```sh
docker compose config --quiet
docker compose up -d --build
docker compose logs -f --tail=100 bot
```

`config --quiet` validates the configuration without printing resolved credentials. Avoid sharing the output of plain `docker compose config` or `docker inspect`, which can include the token.

Wait for the log entry showing a Discord connection. Join a regular voice channel and use `/play` with a public YouTube video URL. Have another listener confirm they hear the audio. Global slash commands can take time to appear. Pressing Ctrl-C while following logs stops the log view; the bot continues running.

## Save YouTube sign-in cookies

YouTube cookies are optional for public content. When sign-in is required, follow the [README's browser sign-in and export procedure](../README.md#youtube-sign-in-saved-for-future-use) and keep the Netscape-format export in a private location outside this repository. Never send it through Discord or include it in an image.

The container stores the imported session in its persistent data volume. You can import cookies while the bot is running; subsequent extraction operations read the new session without a restart.

**macOS or Linux** — stream the export to the importer without mounting your private directory:

```sh
docker compose run --rm -T bot python scripts/import_cookies.py /dev/stdin < /private/path/youtube-export.txt
```

**Windows PowerShell** — temporarily mount only the export, read-only:

```powershell
$cookiePath = (Resolve-Path 'C:\private\youtube-export.txt').Path
docker compose run --rm -T --volume "${cookiePath}:/run/youtube-export.txt:ro" bot python scripts/import_cookies.py /run/youtube-export.txt
```

Replace the example path with the actual export location. Docker Desktop must have permission to read that file. The temporary import container is removed afterward; its named data volume remains. Remove the original export when no longer needed.

The importer writes a private `0600` file owned by the container's non-root account. Check `/youtube_status`, then play content that your account can access. File existence does not prove YouTube still accepts the session. To refresh expired cookies, repeat the export and import procedure.

To remove the saved session while keeping the rest of the volume:

```sh
docker compose run --rm -T bot python -c "from pathlib import Path; import os; Path(os.environ['YOUTUBE_COOKIE_FILE']).unlink(missing_ok=True)"
```

Revoke the session in your Google account as well if you are removing account access. An extraction already in progress may finish using its existing private cookie snapshot.

## Everyday commands

| Task | Command |
| --- | --- |
| Show container state | `docker compose ps` |
| Follow recent logs | `docker compose logs -f --tail=100 bot` |
| Stop gracefully | `docker compose stop` |
| Start the existing container | `docker compose start` |
| Restart the process | `docker compose restart bot` |
| Apply `.env` changes | `docker compose up -d --force-recreate` |
| Rebuild after code/dependency updates | `docker compose up -d --build` |
| Stop and remove the containers | `docker compose down` |

`restart` does not reload changed container environment settings; use `up -d --force-recreate` after editing `.env`. Run one instance per Discord bot token. Do not run a local Python instance or systemd service using the same token at the same time.

The named `bot-data` volume retains cookies across container recreation, builds, and ordinary `docker compose down`. Queue contents remain in memory and reset when the bot restarts. **`docker compose down -v` deletes the named volume and its saved authentication.** Docker prefixes volume names with the Compose project name; changing that project name or moving/renaming the project directory can select a different volume. Keep the project name stable when updating.

Compose runs the image as UID/GID 10001 with a read-only root filesystem, a temporary writable `/tmp`, and the persistent data volume. Compose supplies an init process, drops Linux capabilities, and allows up to 60 seconds for graceful shutdown before forcing termination. Do not add privileged mode or publish ports to troubleshoot ordinary playback failures.

Compose starts with at most five active server players by default, a 2 GiB memory limit, and a 128-task limit. These are starting limits for local use, not a capacity guarantee. Keep concurrent players modest and monitor with `docker stats`. Increase Docker Desktop's memory allocation only if measurements show it is needed. YouTube extraction and voice playback require CPU and memory even when the bot has no web interface.

## Check the container locally

After creating `.env` and building the image, run the offline smoke check using the same service settings and data volume:

```sh
docker compose run --rm -T bot python -m scripts.container_smoke
```

This checks the non-root runtime, audio dependencies, and writable persistent data directory without connecting to Discord or YouTube. It does not verify your bot token, cookie validity, or voice connectivity.

To run formatting checks and the full test suite inside the build environment:

```sh
docker build --target test --tag discord-media-player:test .
```

The `test` build target executes its checks during the build and fails if they fail. The default runtime image excludes test files and development dependencies. These checks complement the live playback acceptance steps below.

## Update and troubleshoot

With a Git checkout, stop the bot, pull the version you intend to run, and rebuild:

```sh
docker compose stop
git pull --ff-only
docker compose up -d --build
```

Keep your `.env` and named volume. To refresh upstream base images during a deliberate update, use `docker compose build --pull` before `docker compose up -d`. Repeat a public-video playback check after updates. YouTube extraction can change independently of the container image; updating the image is not a guarantee that every video will work.

If logs show the voice connection completed followed by `ffmpeg ... return code of 1`, the decoder failed; do not assume Docker's voice networking caused it. Current builds preserve extractor-selected request headers and report a classified playback error without exposing signed URLs. Update the project and rebuild, queue the video again, then inspect the safe error in Discord or the container logs. HTTP 403 means the media server rejected the audio request; a TLS certificate error means the host/container trust configuration needs attention. Keep certificate verification enabled.

For an installation downloaded as a ZIP, download the latest ZIP and replace the `musicbot` folder in your existing project folder with the latest `musicbot` folder. Then run `docker compose up -d --build --force-recreate` from that same project folder. Keep your local `.env` and existing Compose project name so the token and named cookie volume remain available.

If startup fails, inspect `docker compose logs --tail=100 bot`. Check for a missing/invalid bot token, invalid settings, or missing Discord permissions. If the bot connects but no audio is heard, check voice permissions and the host firewall/VPN's UDP access. If extraction fails, try a public recorded video, check the saved YouTube session, and review yt-dlp updates. Keep TLS verification enabled.

A running container is not proof of successful playback. Before relying on it, verify two listeners, playlist import, pause/resume, skip during loading, stop during import, and a container restart. If using cookies, verify authenticated playback again after the restart. Docker does not bypass YouTube content restrictions or account requirements.

## Validation of this setup

The Linux/amd64 image was built and tested with the supplied Dockerfile and Compose configuration. All 101 tests passed inside the test image, with 90.8% combined statement/branch coverage, including real HTTP/HTTPS audio decoding, required request headers, safe HTTP 403 diagnostics, and certificate verification. The runtime smoke check passed with the read-only filesystem and non-root user. Separate containers successfully imported and reopened a synthetic cookie file from the named volume, and an offline bot process shut down cleanly through Docker's SIGTERM signal.

GitHub Actions includes a Docker build-and-smoke job for future changes. Docker Desktop on Windows/macOS and ARM64 builds were not executed during this validation. Live Discord audio and authenticated YouTube playback still need the acceptance checks above with your own credentials.
