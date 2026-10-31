import threading
import time
from concurrent.futures import ThreadPoolExecutor

from app import limits


def test_claude_requests_share_one_cap():
    busy, most, guard = [0], [0], threading.Lock()

    @limits.claude
    def ask(_):
        with guard:
            busy[0] += 1
            most[0] = max(most[0], busy[0])
        time.sleep(0.02)
        with guard:
            busy[0] -= 1

    with ThreadPoolExecutor(40) as pool:  # several searches' worth of requests at once
        list(pool.map(ask, range(80)))
    assert most[0] == limits.CLAUDE_REQUESTS
