# -*- coding: utf-8 -*-
import config
from hcf_backend import HCFQueue
from frontera.core.models import Request
from frontera.contrib.backends.partitioners import FingerprintPartitioner
from time import sleep
import logging


def test_queue():
    logging.basicConfig(level=logging.DEBUG)
    queue = HCFQueue(config.API_KEY, config.PROJECT_ID, config.FRONTIER_NAME, 10000, 1, 1, "", True, FingerprintPartitioner)

    queue.frontier_start()

    r = Request(url="http://scrapinghub.com", meta={b"fingerprint": b"abcdef01234567890", "native": "string test"})
    queue.schedule([("", 0.9, r, True)])
    sleep(4)
    result = queue.get_next_requests(256, 0)
    assert result[0].url == r.url
    assert result[0].meta[b'fingerprint'] == r.meta[b'fingerprint']
    assert result[0].meta["native"] == r.meta["native"]

    queue.frontier_stop()
