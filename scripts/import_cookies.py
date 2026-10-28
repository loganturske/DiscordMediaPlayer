#!/usr/bin/env python3
"""Validate and atomically install a Netscape YouTube cookie export locally."""
import argparse
import http.cookiejar
import os
from pathlib import Path
import tempfile
import warnings


def install(source: Path, destination: Path):
    jar = http.cookiejar.MozillaCookieJar(str(source))
    with warnings.catch_warnings():
        # The stdlib parser can include the offending cookie line in warnings.
        warnings.simplefilter('ignore')
        jar.load(ignore_discard=True, ignore_expires=True)
    cookies = [c for c in jar if c.domain.lstrip('.') in {'youtube.com', 'google.com'}
               or c.domain.endswith(('.youtube.com', '.google.com'))]
    if not cookies:
        raise ValueError('No YouTube/Google cookies found in the Netscape-format export.')
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.cookies-', dir=destination.parent)
    try:
        os.close(fd)
        output = http.cookiejar.MozillaCookieJar(temporary)
        for cookie in cookies:
            output.set_cookie(cookie)
        output.save(ignore_discard=True, ignore_expires=True)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--destination', type=Path, default=Path(os.environ.get('YOUTUBE_COOKIE_FILE', 'data/youtube-cookies.txt')))
    args = parser.parse_args()
    try:
        install(args.source, args.destination)
    except (ValueError, OSError, http.cookiejar.LoadError) as error:
        # Cookie parsers may include input data in their exception text.
        raise SystemExit(f'Cookie import failed ({type(error).__name__}); check the file format and permissions.') from None
    print('Cookie session saved privately. New extractions use it without restarting the bot.')


if __name__ == '__main__':
    main()
