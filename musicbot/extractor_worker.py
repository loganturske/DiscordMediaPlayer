"""Private JSON-lines worker; isolates blocking yt-dlp/JS work from the gateway loop."""

import json
import sys
from dataclasses import asdict
from pathlib import Path

from .media import Media


def emit(message):
    print(json.dumps(message), flush=True)


def main():
    try:
        request = json.loads(sys.stdin.readline(16384))
        media = Media(Path(request['cookie_file']), snapshot_root=Path(request['snapshot_root']))
        if request['mode'] == 'import':
            for track in media.entries(request['url'], request['requester'], request['limit']):
                emit({'type': 'track', **asdict(track)})
            emit({'type': 'done'})
        elif request['mode'] == 'resolve':
            stream = media.resolve_sync(request['url'])
            emit({'type': 'stream', 'url': stream.url, 'headers': dict(stream.headers)})
        else:
            raise ValueError('Unknown operation')
    except Exception:
        # Provider exceptions can contain cookies or signed URLs. Never serialize them.
        emit({'type': 'error'})
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
