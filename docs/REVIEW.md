# Production hardening review — October 28, 2026

The review started from commit `e417d7d`. The resulting changes improve reliability and operating controls, but they are not evidence of a live production deployment.

## Findings addressed

| Finding | Change |
| --- | --- |
| Large imports could occupy every shared extractor slot and delay playback. | Independent bounded pools reserve capacity for audio resolution. |
| Thread-backed extraction could not be reliably stopped; closing its generator during extraction was unsafe. | Isolated workers with lookup/import deadlines, cancellation, process-group cleanup on Linux, and parent-owned cookie snapshot cleanup. |
| Shutdown could wait indefinitely, and overlapping voice connects/disconnects could corrupt player ownership. | Bounded player cleanup, shared shutdown tasks, guild connection locks, old-player cleanup before reconnecting, and SIGTERM handling. |
| A late audio callback could complete another track's playback event. | A distinct completion future for each track, with stale callback regression coverage. |
| Slow or failed Discord notifications could stall music. | A bounded notification queue with delivery deadlines; playback continues independently. |
| Empty-channel timers could be reset by unrelated events. | Timers persist until listeners return; unrelated channel events are ignored. |
| Unbounded settings and unrestricted command bursts could consume resources. | Validated configuration ranges, concurrent-player capacity, per-user cooldowns/import caps, and an optional server allowlist. |
| Escaped queue titles could exceed Discord's message size limit. | Payload bounds after markdown escaping and paginated iteration without copying whole queues. |
| FFmpeg's stderr configuration could crash a library forwarding thread. | A real null-device file handle and an actual failed-FFmpeg regression test. |
| Production startup, service supervision, and CI were undocumented. | Non-root systemd deployment, persistent cookie storage, operating procedures, and Python 3.12/3.13 CI configuration. |

## Efficiency and maintainability follow-up

Capped playlist imports now stop before requesting the next entry, avoiding an unnecessary remote page fetch at the limit. Immutable slotted track records reduce metadata overhead for large queues. Server lifecycle locks are allocated once and reused. Queue consumption remains constant-time, and queue pages avoid copying the entire queue.

Ruff enforces consistent formatting, import order, and basic correctness checks in CI. Public lifecycle APIs and configuration construction are documented; [ARCHITECTURE.md](ARCHITECTURE.md) describes ownership, cancellation, protocol boundaries, and extension guidance. These changes do not establish end-to-end throughput or hosting capacity.

## Validation in the current environment

- 82 tests passed on Python 3.12, including real local extractor-worker lifecycle and FFmpeg/Opus checks.
- Line coverage: 860 / 918 statements, 93.7%.
- Branch coverage: 219 / 262 branches, 83.6%.
- Combined coverage: 91.4%; the configured CI floor is 85%.
- Dependency consistency (`pip check`) and whitespace checks passed.
- systemd service syntax was checked with installation paths mapped to this checkout. The service was not installed or started.

The Python 3.13 CI job has been configured but was not executed here. Worker protocol tests use controlled local processes, and YouTube/Discord network interactions use fakes. Tests do not establish current cookie validity or audible playback through Discord.

## Release gates and current limits

Complete the live checks in [DEPLOYMENT.md](DEPLOYMENT.md) on the intended voice-capable host: public and authenticated playback, two listeners, large imports, command controls, shutdown, and restart. Load-test the intended number of simultaneous servers before raising capacity. The supplied service environment starts with five players and a 2 GiB service limit; these are starting settings, not a capacity guarantee.

Queue state is still in memory. YouTube authentication is one host-managed session shared by permitted servers. Transitive dependencies are not fully locked, and Windows cannot provide the same process-group cleanup guarantee as the Linux production setup. The service does not provide an automated live-health endpoint or a custom voice-reconnect policy.

See [ROADMAP.md](ROADMAP.md) for prioritized additional features and their acceptance criteria.
