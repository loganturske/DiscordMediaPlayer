# Developer architecture

The bot runs one Discord client and event loop. Each active server has one `Player`; all players share a `Media` coordinator. Blocking YouTube extraction runs in subprocesses, and FFmpeg supplies audio to discord.py's voice thread.

## Module responsibilities

| Module | Responsibility |
| --- | --- |
| `musicbot/config.py` | Parse environment settings once and reject invalid ranges before connecting. Credentials stay outside the settings object. |
| `musicbot/bot.py` | Register slash commands, validate server/channel access, coordinate voice connections, format Discord messages, and handle process shutdown. |
| `musicbot/player.py` | Own a server's FIFO queue, import jobs, playback loop, notifications, and shutdown. |
| `musicbot/media.py` | Validate URLs, limit extraction concurrency, exchange messages with workers, enforce deadlines, and clean up processes and private files. |
| `musicbot/extractor_worker.py` | Run one yt-dlp operation and emit a small JSON-lines response stream. |
| `scripts/import_cookies.py` | Validate and atomically install a private host-managed YouTube cookie export. |

## From a command to audio

1. `/play` validates the link and the caller's server and voice membership. A per-server lock protects voice connection creation and queue submission. Global capacity includes connections still being established.
2. `Player.submit()` appends an import job with the queue's current generation. A single importer per player processes jobs in submission order.
3. `Media.import_tracks()` starts a worker that yields video metadata incrementally. The player appends canonical video URLs, titles, and requester IDs; it does not store expiring audio URLs.
4. The playback loop removes one track and resolves its audio URL immediately before use. Resolution has separate capacity from imports, so long playlists cannot occupy every playback extraction slot.
5. FFmpeg decodes the stream to stereo 48 kHz PCM. discord.py consumes 20 ms frames and encodes/transmits Opus through the voice connection. Audio is streamed, not downloaded to a playlist cache.
6. The voice thread reports completion with `loop.call_soon_threadsafe`. Each track owns a distinct completion future, so a late callback cannot complete the next track.

Notifications use a separate bounded delivery loop. Discord text-channel failures or rate limits do not pause the audio loop.

## Ownership and cancellation rules

Queue and job mutations occur only on the event loop. Keep the state checks and mutations in `Queue.append()`, `Player.submit()`, and the start of `Player.close()` free of intervening `await` calls. The guild lock protects operations that do await while connecting or disconnecting; queue mutation itself does not need a thread lock.

`Queue.clear()` increments a generation counter. An import may append only when its captured generation still matches, preventing results from an old import from repopulating a stopped queue. `/stop` also cancels the active import and current resolution; `/skip` cancels only the current resolution/playback. Partial import results remain queued after an extraction failure.

`Player.close()` marks the player closed before awaiting cleanup, then shares one shutdown task among callers. The bot waits for an old player's shutdown before replacing its voice connection. Empty-channel timers keep their original deadline until a human returns; unrelated voice events do not postpone departure.

`Media` owns each worker's temporary directory. Workers use a private cookie snapshot, so concurrent extraction cannot rewrite the durable session. Cancellation kills the worker process group on Linux, reaps the process, and removes the snapshot. A shared cleanup task prevents multiple callers from draining the same subprocess pipe. Cleanup task records use weak keys so completed workers do not accumulate indefinitely.

SIGINT and SIGTERM unwind the bot context. Bot shutdown closes players, then the media coordinator, then the Discord client. The systemd service adds a final process-group shutdown deadline. Windows terminates the worker itself but does not provide the same descendant-process cleanup guarantee as Linux.

## Worker protocol

The parent starts `python -m musicbot.extractor_worker` with stdin/stdout pipes. It sends one JSON object followed by a newline, then closes stdin. Requests contain `mode` (`import` or `resolve`), `url`, `requester`, `limit`, `cookie_file`, and `snapshot_root`. The Discord token is excluded from the worker environment; cookie contents are never passed in arguments or protocol messages.

Worker stdout is reserved for JSON lines:

| Message | Fields and meaning |
| --- | --- |
| `track` | `url`, `title`, `requested_by`: one import result. |
| `done` | Import completed successfully. |
| `stream` | `url`: one resolved audio stream. The parent validates the HTTPS Google video CDN destination before returning it to playback. |
| `error` | Extraction failed. Provider exception text is deliberately omitted because it can contain session data or signed URLs. |

The parent checks message types and required fields. Unexpected EOF, malformed output, inactivity, or a deadline expiration fails the operation while preserving previously queued tracks. Do not add ordinary logging to worker stdout or print raw provider responses.

## Resource bounds and cost

| Resource | Bound or behavior |
| --- | --- |
| Waiting tracks | Default 10,000 per server; configurable up to 100,000. Queue metadata uses memory proportional to this limit. |
| Queue append / next track | `deque.append` and `deque.popleft`: constant-time operations. |
| Queue page | `islice` avoids copying the entire queue. Reaching a page still takes time proportional to its offset; the displayed page contains at most 10 tracks. |
| Import jobs | At most 20 waiting per server plus one active job; each user may have at most five pending jobs, counting their active import. |
| Extractor processes | At most two imports and three audio resolutions across the bot. A process is created per operation to permit reliable cancellation; this has startup cost. |
| Extraction deadlines | 60 seconds for an audio lookup, 60 seconds without an import response, and 15 minutes for an active import. Waiting for an import slot is separately bounded at 15 minutes. |
| Notifications | 50 queued per player, with a five-second delivery deadline. Excess notifications are dropped with a log warning. |
| Voice players | Default application cap 20; the supplied 2 GiB service example starts at five. These values are limits, not measured capacity guarantees. |

No benchmark establishes a supported number of simultaneous listeners or servers. Measure CPU, memory, extraction latency, and audio continuity on the intended host before increasing capacity. Cache metadata only with a clear expiry policy; caching signed audio URLs can make long queues fail when playback eventually starts.

## Extending the bot

Keep Discord authorization and interaction responses in `bot.py`, queue/playback behavior in `player.py`, and provider operations in `media.py`. Add a settings field with a validated range when a limit needs operator configuration. Keep new process cleanup and cancellation paths idempotent.

For a new command, test voice-channel authorization, server isolation, empty-state behavior, and failure responses. For changes to import or playback state, add a regression that forces the relevant interleaving rather than relying on timing luck. Use controlled events and fake voice/media clients; use real local subprocesses when testing process lifecycle.

Run the documented coverage command from the repository root:

```sh
.venv/bin/python -W error::ResourceWarning -m coverage run -m unittest discover -s tests -v
.venv/bin/python -m coverage report
```

Tests never need real tokens or cookies. Live Discord and YouTube acceptance remains separate; follow [DEPLOYMENT.md](DEPLOYMENT.md) before release. Proposed capabilities and acceptance criteria live in [ROADMAP.md](ROADMAP.md).
