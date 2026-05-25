from datetime import datetime

import pytest

pytest.importorskip("apscheduler")

from alex.scheduler import _next_cron_time


def test_next_cron_every_5_minutes():
    base = datetime.now().astimezone().replace(second=30, microsecond=0)
    next_ts = _next_cron_time(base.timestamp(), "*/5 * * * *")
    nxt = datetime.fromtimestamp(next_ts).astimezone()

    assert next_ts > base.timestamp()
    assert nxt.minute % 5 == 0
