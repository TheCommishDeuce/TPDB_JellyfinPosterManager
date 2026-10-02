"""Check app responsiveness without relying on Jellyfin or TPDb availability."""
import json
import os
import sys
from urllib.error import URLError
from urllib.request import urlopen


def check():
    port = int(os.environ.get('WEB_PORT', '5001'))
    with urlopen(f'http://127.0.0.1:{port}/health/live', timeout=3) as response:
        if response.status != 200 or json.load(response).get('status') != 'alive':
            raise ValueError('Unexpected liveness response')


if __name__ == '__main__':
    try:
        check()
    except (OSError, URLError, ValueError) as error:
        print(f'App health check failed: {error}', file=sys.stderr)
        sys.exit(1)
