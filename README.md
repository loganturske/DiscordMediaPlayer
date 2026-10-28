# DiscordMediaPlayer

A self-hosted Discord bot that plays YouTube video and YouTube Music playlist audio in a shared voice channel. Each server has an independent FIFO queue. Everyone in the bot's voice channel can add music and control playback; no DJ role or administrator permission is required.

## Run the bot

Requires Python 3.12+, FFmpeg, libopus, and Node.js 20+ (for YouTube's JavaScript challenges). On Debian/Ubuntu, install system prerequisites with `sudo apt-get install python3-venv ffmpeg libopus0` and install Node.js 20+ through your usual supported package source.

```sh
cd /path/to/DiscordMediaPlayer
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export DISCORD_TOKEN='your-bot-token'
# Optional: immediate slash-command registration in one development server.
export DISCORD_GUILD_ID='your-server-id'
.venv/bin/python -m musicbot
```

Create an application and bot in the [Discord Developer Portal](https://discord.com/developers/applications). Keep the token private. Invite it with the `bot` and `applications.commands` scopes and **View Channels, Send Messages, Connect, Speak** permissions. Grant those permissions in the command text channel and voice channel as appropriate. No privileged gateway intents or Message Content intent are needed. The bot must be allowed to send messages to the text channel where `/play` is invoked.

Without `DISCORD_GUILD_ID`, commands are registered globally and may take time to appear. When switching from development to global registration, remove old guild command registrations in your development server if Discord shows duplicates.

The bot is a long-running process: use your process supervisor to restart it on failure. Run only one instance per bot token. Queue state is in memory and resets on restart. Cookie authentication is stored separately and survives restarts when its file is retained. `.env.example` documents environment variables; `.env` files are **not** loaded automatically.

## Commands

| Command | Behavior |
| --- | --- |
| `/play url` | Join your voice channel and append a YouTube video or playlist. Accepts `youtube.com`, `youtu.be`, and `music.youtube.com` HTTPS links. A video link containing `list=` imports its playlist; remove `list=` to add only that video. |
| `/queue page` | Show the current track, pending imports, and 10 queued tracks per page. Anyone in the server can view it. |
| `/skip` | Skip the current track, even while its audio URL is loading. |
| `/pause`, `/resume` | Pause or resume current audio. |
| `/stop` | Stop audio, clear queued tracks, and invalidate in-flight imports. Stay connected. |
| `/leave` | Clear everything and disconnect. |
| `/youtube_status` | Privately show whether a host cookie file exists; this does not verify its validity. |

Join a regular voice channel before using playback controls. Commands from other voice channels cannot move or interrupt an active player. Stage channels and live streams are not supported. The player leaves after five idle minutes (configurable).

### Large playlists

Playlist metadata is fetched lazily in a background thread, and entries are appended incrementally. Playback starts after the first result; only the current track's streaming URL is resolved, immediately before playback so URLs do not expire while waiting in the queue. No whole-playlist downloads or audio files are stored.

Imports run in submission order within each server. Each import is capped at 10,000 source entries, and the waiting queue is capped at 10,000 tracks by default. Up to 20 additional import requests can wait per server; excess requests are rejected. `/stop` cancels the remainder, including results arriving from an already-running YouTube request. Private/deleted/unavailable entries can fail or be omitted by YouTube; playback reports failures and continues. Failed imports retain entries already added. The bot reports how many entries it added; an import is not a promise that every item is playable.

## YouTube sign-in saved for future use

Public videos often work without authentication. When YouTube requires sign-in, use an account you control with access to that content. There is no password prompt or Google login inside Discord, and Google OAuth alone does not grant yt-dlp access to restricted video audio.

1. On a trusted local computer, sign in to YouTube in a separate browser profile. Follow [yt-dlp's YouTube cookie export guidance](https://github.com/yt-dlp/yt-dlp/wiki/Extractors#exporting-youtube-cookies), including its advice about session rotation. Export in **Netscape cookies.txt format**, using a trusted tool. Treat the export like an account password.
2. Transfer the export securely to the bot host, outside the checkout. Never upload cookies to Discord, commit them, or paste them into chat/logs.
3. On the bot host, install the session:

   ```sh
   .venv/bin/python scripts/import_cookies.py /private/path/youtube-export.txt
   ```

   This validates the format, retains only Google/YouTube-domain cookies, and atomically saves a private `0600` file at `data/youtube-cookies.txt`. It does not verify whether Google still accepts the session. Set `YOUTUBE_COOKIE_FILE` in both the importer and bot processes to choose another persistent path. Remove the temporary export securely after importing.
4. Queue the restricted video again. New extraction operations automatically read the saved session, with no restart required. The durable session is never exposed in Discord or rewritten by concurrent extraction jobs. Keep the `data` directory on persistent storage across deployments.

Cookies can expire or be revoked; re-export and run the importer again when needed. Delete the saved file and revoke the browser session in your Google account to remove access. This is host-managed authentication shared by all servers served by this bot: use a dedicated account, and invite the bot only to trusted servers. Anyone able to queue content can request content accessible to that account. Cookie import is deliberately unavailable through Discord commands.

Authentication cannot bypass membership, payment, regional restrictions, anti-bot challenges, or content permissions. Some YouTube environments require additional provider-specific configuration (such as PO tokens); this bot does not fabricate those or guarantee all videos will play. Use content you have permission to stream and comply with the services' terms.

## Configuration and hosting

| Variable | Default | Purpose |
| --- | --- | --- |
| `DISCORD_TOKEN` | required | Discord bot token, supplied through your host's secret manager or process environment. |
| `DISCORD_GUILD_ID` | unset | Optional development server for immediate command sync. |
| `YOUTUBE_COOKIE_FILE` | `data/youtube-cookies.txt` | Private persistent cookie export. Relative paths resolve from the working directory. |
| `MAX_QUEUE_SIZE` | `10000` | Maximum waiting tracks per server and maximum entries inspected per import. Must be positive. |
| `IDLE_TIMEOUT` | `300` | Seconds before leaving an empty queue. Must be positive. |

The host needs HTTPS/WebSocket access to Discord, YouTube, and YouTube media CDNs, plus Discord voice UDP connectivity. A restricted cloud coding environment may support tests but lack the UDP/network access needed for live playback. Use a VM or other host that explicitly supports Discord voice; a web-only deployment is insufficient. FFmpeg must also be able to reach YouTube media URLs. Do not disable TLS verification to troubleshoot network failures.

YouTube changes frequently. If extraction breaks, review official yt-dlp release notes, update its pin in `requirements.txt`, reinstall, and test playback again. Cookie validity and successful extraction cannot be established by the unit tests.

## Development and validation

Use the existing checkout; cloud tasks are already isolated and do not need a new Git worktree.

```sh
.venv/bin/python -m pip check
.venv/bin/python -m unittest discover -s tests -v
```

Tests cover queue ordering and limits, lazy 10,000-entry metadata handling, cancellation of late import results, skip during stream resolution, playback failure recovery, idle disconnect, server isolation, voice-channel authorization, cookie persistence/permissions, slash command registration, and real FFmpeg PCM decoding/Opus encoding. Network and voice behavior are faked in queue tests.

For live acceptance, start with a bot token on a voice-capable host, invite a second listener, queue a public video, import a playlist, and verify both listeners hear audio in queue order. Test pause/resume/skip and `/stop` during import. Import a permitted authenticated cookie session and test content requiring sign-in, then restart the bot and repeat to verify persistent authentication. Never use real cookies or tokens in test fixtures.
