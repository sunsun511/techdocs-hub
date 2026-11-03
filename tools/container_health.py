"""Probe the internal listener using the application's configured Host."""

import os
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def main():
    # Compose may map a different public port; never connect to that port here.
    host = urlsplit(os.environ["TECHDOCS_ORIGIN"]).netloc
    request = Request("http://127.0.0.1:8377/api/health", headers={"Host": host})
    with urlopen(request, timeout=3) as response:
        if response.status != 200:
            raise RuntimeError("Health endpoint did not return HTTP 200")


if __name__ == "__main__":
    main()
