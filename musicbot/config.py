"""Validated process configuration; never include credentials in representations."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


def integer(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    """Read a bounded integer without echoing invalid configuration values."""
    try:
        value = int(env.get(name, str(default)))
    except ValueError:
        raise ValueError(f'{name} must be an integer.') from None
    if not low <= value <= high:
        raise ValueError(f'{name} must be between {low} and {high}.')
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """Non-secret settings validated once at startup."""

    cookie_file: Path
    queue_size: int
    idle_timeout: int
    empty_timeout: int
    max_players: int
    play_cooldown: int
    allowed_guilds: frozenset[int]
    dev_guild: int | None
    sync_commands: bool

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        guilds = set()
        for value in env.get('ALLOWED_GUILD_IDS', '').split(','):
            if not value.strip():
                continue
            try:
                number = int(value)
                if number <= 0:
                    raise ValueError
                guilds.add(number)
            except ValueError:
                raise ValueError(
                    'ALLOWED_GUILD_IDS must contain comma-separated positive server IDs.'
                ) from None
        sync = env.get('SYNC_COMMANDS', 'true').lower()
        if sync not in {'true', 'false'}:
            raise ValueError('SYNC_COMMANDS must be true or false.')
        dev_guild = None
        if env.get('DISCORD_GUILD_ID'):
            dev_guild = integer(env, 'DISCORD_GUILD_ID', 0, 1, 2**64 - 1)
        return cls(
            cookie_file=Path(env.get('YOUTUBE_COOKIE_FILE', 'data/youtube-cookies.txt')),
            queue_size=integer(env, 'MAX_QUEUE_SIZE', 10000, 1, 100000),
            idle_timeout=integer(env, 'IDLE_TIMEOUT', 300, 10, 86400),
            empty_timeout=integer(env, 'EMPTY_CHANNEL_TIMEOUT', 60, 5, 3600),
            max_players=integer(env, 'MAX_PLAYERS', 20, 1, 100),
            play_cooldown=integer(env, 'PLAY_COOLDOWN_SECONDS', 5, 1, 300),
            allowed_guilds=frozenset(guilds),
            dev_guild=dev_guild,
            sync_commands=sync == 'true',
        )
