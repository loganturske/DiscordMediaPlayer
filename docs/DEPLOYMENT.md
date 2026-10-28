# Deploy on a Linux voice-capable host

The service definition in `deploy/discord-media-player.service` runs the bot as an unprivileged account with read-only application files, a private persistent cookie directory, restart-on-failure behavior, and a 60-second shutdown deadline. It is intended for a systemd host with direct Discord voice UDP access and HTTPS/WebSocket access to Discord, YouTube, and YouTube media CDNs. A web-only platform or restricted coding environment may not support voice connections.

These are operator-run installation instructions. The service has not been installed or connected to a live Discord server by the automated tests.

## First installation

Install Python 3.12+, FFmpeg, libopus, Git, and **system-wide Node.js 20+**. On Ubuntu 24.04:

```sh
sudo apt-get update
sudo apt-get install python3-venv ffmpeg libopus0 git
# Install Node.js 20+ from a supported distribution/source, then verify:
node --version
```

A Node.js binary under another user's home directory is not accessible to the hardened service. Use a system-wide executable on the service's PATH.

Create a dedicated account and install a reviewed project version in `/opt`. For an existing deployment, use the update procedure below instead of replacing its files.

```sh
sudo useradd --system --user-group --home-dir /var/lib/discord-media-player --shell /usr/sbin/nologin discord-media
sudo git clone https://github.com/loganturske/DiscordMediaPlayer.git /opt/DiscordMediaPlayer
cd /opt/DiscordMediaPlayer
# Optionally select an exact reviewed commit before installing.
sudo python3 -m venv .venv
sudo .venv/bin/python -m pip install -r requirements.txt
sudo .venv/bin/python -m pip check
sudo chown -R root:root /opt/DiscordMediaPlayer
sudo chmod -R go-w /opt/DiscordMediaPlayer
```

Create the bot in the Discord Developer Portal and invite it with the scopes and permissions described in the main README. Install an empty environment template, then edit it locally to supply the token. Do not place credentials in shell history or source control.

```sh
sudo install -o root -g root -m 0600 deploy/discord-media-player.env.example /etc/discord-media-player.env
sudoedit /etc/discord-media-player.env
sudo install -o root -g root -m 0644 deploy/discord-media-player.service /etc/systemd/system/discord-media-player.service
sudo systemctl daemon-reload
sudo systemctl enable --now discord-media-player
sudo systemctl status discord-media-player
sudo journalctl -u discord-media-player -n 50 --no-pager
```

Set `ALLOWED_GUILD_IDS` to the intended server IDs, particularly when using a shared authenticated YouTube account. Keep `SYNC_COMMANDS=true` for first deployment and whenever command definitions change; it can be set to `false` between command changes. Use `DISCORD_GUILD_ID` only for a development server; leaving it empty registers commands globally.

The service's `StateDirectory` creates `/var/lib/discord-media-player` with private permissions. Keep this directory across code updates. Application code and the virtual environment remain owned by root and cannot be modified by the bot account.

## Install or refresh a YouTube session

Follow the main README's browser sign-in and Netscape cookie export procedure. The token and cookies are different credentials. After securely transferring the export to the host, import it as the service account so the resulting private file is readable by the bot:

```sh
# Supply the cookie file through stdin so the service account needs no access
# to your private home directory. Enter the actual local export path here.
sudo -u discord-media /opt/DiscordMediaPlayer/.venv/bin/python \
  /opt/DiscordMediaPlayer/scripts/import_cookies.py /dev/stdin \
  --destination /var/lib/discord-media-player/youtube-cookies.txt \
  < /private/path/youtube-export.txt
```

New extractions pick up the updated cookie file automatically. Verify authentication using content your account can access; file existence alone does not prove sign-in still works. Remove the transfer copy when no longer needed. Backups of the state directory contain account credentials and need equivalent access controls.

## Verify before inviting listeners

1. Confirm the process logs a Discord connection and command registration succeeds.
2. Join a regular voice channel and queue a public video. Have a second listener confirm audible playback.
3. Import a playlist, queue another track as the second listener, and check ordering.
4. Test pause/resume, skip during loading, and stop during a large import. Confirm the queue stays empty after stop.
5. Confirm a member in another voice channel cannot interrupt playback and another server cannot access this server's queue.
6. If cookies are configured, play permitted content requiring sign-in, restart the service, and repeat.
7. Use `systemctl stop` and confirm the bot disconnects, its extractor/FFmpeg processes exit, and no state file is lost. Start it again and verify recovery. Queue contents currently reset on restart.

Automated tests exercise local audio encoding and fake voice clients. They do not replace this live acceptance check or establish YouTube account validity.

## Operations and updates

Use `journalctl -u discord-media-player -f` to inspect logs. Playback and extraction errors intentionally omit signed media URLs and cookies. Extractor workers do not inherit the Discord bot token. Keep log access limited to operators. A running PID is not proof of audible playback; monitor actual command and voice behavior.

The unit limits the service to 2 GiB and 128 tasks. These are starting limits for a modest deployment, not capacity guarantees. Measure memory, CPU, extractor latency, and concurrent guild load on the intended host before raising `MAX_PLAYERS` or resource limits. A host OOM kill can interrupt playback; systemd restarts failed services. Repeated startup failures are rate-limited, so fix the cause and run `systemctl reset-failed discord-media-player` before restarting.

For an update, record the current commit, stop the service, fetch and check out the reviewed release/commit as an administrator, reinstall `requirements.txt`, run `pip check` and the test suite in a development environment, then restart and repeat a public-video smoke check. Keep the previous commit and matching dependency environment available for rollback. Do not run `git pull` from the service process or grant the bot write access to its own code.

YouTube changes frequently. Review yt-dlp updates when extraction fails, update the repository's dependency pin in a tested change, and redeploy. Re-export expired cookies when authentication fails; do not disable TLS verification or log session values while troubleshooting.

The dependency file pins direct runtime packages; transitive versions still resolve during installation. For a controlled release, retain the tested virtual environment or produce and verify a platform-specific dependency lock/artifact in your release pipeline. No release artifact or fresh-host deployment has been certified by this code review.
