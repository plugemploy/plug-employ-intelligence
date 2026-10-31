"""App-wide caps on outside work, shared by every search running at once. With a few people searching, each one
goes as fast as before; with many, everyone keeps moving, just a little slower, instead of overloading the server
or Claude. There's no waiting list: each search's leads still show up as they're found."""

import functools
import threading

WEBSITE_READS = 16  # business websites being read at once, across the whole app
CLAUDE_REQUESTS = 12  # Claude requests at once, across the whole app

_websites = threading.BoundedSemaphore(WEBSITE_READS)
_claude = threading.BoundedSemaphore(CLAUDE_REQUESTS)


def _limited(slots: threading.BoundedSemaphore):
    def wrap(fn):
        @functools.wraps(fn)
        def run(*args, **kwargs):
            with slots:
                return fn(*args, **kwargs)
        return run
    return wrap


website = _limited(_websites)
claude = _limited(_claude)
