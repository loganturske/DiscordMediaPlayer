import unittest

from musicbot.config import Settings


class ConfigTests(unittest.TestCase):
    def test_defaults_and_explicit_values(self):
        default = Settings.from_env({})
        self.assertEqual(default.queue_size, 10000)
        self.assertEqual(default.allowed_guilds, frozenset())
        settings = Settings.from_env(
            {
                'MAX_QUEUE_SIZE': '20',
                'DISCORD_GUILD_ID': '123',
                'ALLOWED_GUILD_IDS': ' 123,456,123,',
                'SYNC_COMMANDS': 'false',
            }
        )
        self.assertEqual(settings.queue_size, 20)
        self.assertEqual(settings.allowed_guilds, {123, 456})
        self.assertEqual(settings.dev_guild, 123)
        self.assertFalse(settings.sync_commands)

    def test_invalid_values_fail_without_echoing_input(self):
        for env in (
            {'MAX_QUEUE_SIZE': '0'},
            {'MAX_QUEUE_SIZE': '100001'},
            {'IDLE_TIMEOUT': '-1'},
            {'MAX_PLAYERS': '101'},
            {'EMPTY_CHANNEL_TIMEOUT': '0'},
            {'SYNC_COMMANDS': 'maybe'},
            {'DISCORD_GUILD_ID': '-5'},
            {'ALLOWED_GUILD_IDS': '1,private-value'},
            {'PLAY_COOLDOWN_SECONDS': 'private-value'},
        ):
            with self.subTest(env=env), self.assertRaises(ValueError) as error:
                Settings.from_env(env)
            self.assertNotIn('private-value', str(error.exception))
