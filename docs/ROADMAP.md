# Suggested next features

These are proposed features, not capabilities implemented by the current bot.

| Priority | Feature | Benefit and acceptance criteria |
| --- | --- | --- |
| 1 | Persistent queues and recovery | Store queue metadata per server in SQLite with migrations; restore on restart without reusing expired audio URLs or silently auto-joining a channel. Test crash recovery and server isolation. |
| 1 | Fair queuing and per-user limits | Prevent one large import from dominating listening; configurable request caps and requester round-robin ordering, with transparent queue positions. |
| 1 | Queue editing | Add remove, move, shuffle, clear-own, and play-next controls with rules for requesters and moderators. Test changes during imports and playback. |
| 1 | Diagnostics and operational metrics | Expose authenticated health/readiness and bounded metrics for import latency, queue depth, failures, active voice sessions, and reconnects; redact tokens, cookies, and signed URLs. |
| 2 | Vote-to-skip and configurable DJ controls | Keep ordinary queuing open while allowing each server to choose voting rules, moderator overrides, and who can stop or clear everyone's music. |
| 2 | Playback controls in a message | Add now-playing embeds and buttons for pause, resume, skip, and queue pages, with the same voice-channel authorization as slash commands. |
| 2 | Search and selection | Let users search by song/video title and choose from a short private result list before enqueueing; preserve URL validation and request limits. |
| 2 | Saved playlists and favorites | Store named server playlists separately from active queues, with owner/moderator permissions, import/export, and deleted-video handling. |
| 2 | Reliable reconnect policy | Recover transient Discord voice disconnects with bounded backoff and clear behavior for the interrupted track; test disconnects without duplicating audio or reviving stopped queues. |
| 3 | Volume, seek, and repeat modes | Add bounded volume, seek for recorded media, and explicit track/queue repeat settings without allowing indefinite resource use. |
| 3 | Per-server authenticated media profiles | Isolate account access across servers with an operator-managed credential store, revocation, access audit, and encryption at rest. Never accept account cookies through ordinary Discord chat. |
| 3 | Multi-process scaling | Add durable shared state and explicit ownership of each server's voice session before sharding or running replicas with the same application. |

Before expanding scope, complete live voice acceptance on the intended hosting network and run a multi-server load test. Cookie renewal, Discord voice transport, and YouTube extraction depend on external services that the unit test suite cannot certify.
