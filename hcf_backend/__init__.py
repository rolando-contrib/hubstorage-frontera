import logging
from collections import defaultdict
from datetime import datetime
from json import loads
from time import time, sleep

import requests as requests_lib
from frontera import Request
from frontera.contrib.backends import CommonBackend
from frontera.contrib.backends.memory import MemoryStates, MemoryMetadata
from frontera.contrib.backends.remote.codecs.json import _convert_and_save_type, _convert_from_saved_type
from frontera.core.components import Queue
from frontera.utils.misc import load_object
from hubstorage import HubstorageClient
import six


logger = logging.getLogger(__name__)


class HCFStates(MemoryStates):

    def __init__(self, auth, project_id, colname, cache_size_limit, cleanup_on_start):
        super(HCFStates, self).__init__(cache_size_limit)
        self._hs_client = HubstorageClient(auth=auth)
        self.projectid = project_id
        project = self._hs_client.get_project(self.projectid)
        self._collections = project.collections
        self._colname = colname + "_states"
        self.logger = logger.getChild(self.__class__.__name__)

        if cleanup_on_start:
            self._cleanup()

    @classmethod
    def from_manager(cls, manager):
        s = manager.settings
        return cls(s.get('HCF_AUTH', None),
                   s.get('HCF_PROJECT_ID'),
                   s.get('HCF_FRONTIER'),
                   s.get('HCF_STATES_CACHE_SIZE', 20000),
                   s.get('HCF_CLEANUP_ON_START', False))

    def _cleanup(self):
        while True:
            nextstart = None
            params = {'method':'DELETE',
                      'url':'https://storage.scrapinghub.com/collections/%s/s/%s' % (self.projectid, self._colname),
                      'auth':self._hs_client.auth}
            if nextstart:
                params['prefix'] = nextstart
            response = self._hs_client.session.request(**params)
            if response.status_code != 200:
                self.logger.error("%d %s", response.status_code, response.content)
                self.logger.info(params)
            try:
                r = loads(response.content.decode('utf-8'))
                self.logger.debug("Removed %d, scanned %d", r["deleted"], r["scanned"])
                nextstart = r.get('nextstart')
            except ValueError as ve:
                self.logger.debug(ve)
                self.logger.debug("content: %s (%d)" % (response.content, len(response.content)))
            if not nextstart:
                break

    def frontier_start(self):
        self._store = self._collections.new_store(self._colname)

    def frontier_stop(self):
        self.logger.debug("Got frontier stop.")
        self.flush()
        self._hs_client.close()

    def _hcf_fetch(self, to_fetch):
        finished = False
        i = iter(to_fetch)
        while True:
            prepared_keys = []
            while True:
                try:
                    prepared_keys.append("key=%s" % next(i))
                    if len(prepared_keys) >= 32:
                        break
                except StopIteration:
                    finished = True
                    break

            if not prepared_keys:
                break

            prepared_keys.append("meta=_key")
            params = {'method':'GET',
                      'url':'https://storage.scrapinghub.com/collections/%s/s/%s' % (self.projectid, self._colname),
                      'params':str('&').join(prepared_keys),
                      'auth':self._hs_client.auth}
            start = time()
            response = self._hs_client.session.request(**params)
            self.logger.debug("Fetch request time %f ms", (time()-start) * 1000)
            if response.status_code != 200:
                self.logger.error("%d %s", response.status_code, response.content)
                self.logger.info(params)
            for line in response.content.decode('utf-8').split('\n'):
                if not line:
                    continue
                try:
                    yield loads(line)
                except ValueError as ve:
                    self.logger.debug(ve)
                    self.logger.debug("content: %s (%d)" % (line, len(line)))
            if finished:
                break

    def fetch(self, fingerprints):
        to_fetch = [f for f in fingerprints if f not in self._cache]
        self.logger.debug("cache size %s" % len(self._cache))
        self.logger.debug("to fetch %d from %d" % (len(to_fetch), len(fingerprints)))
        if not to_fetch:
            return
        count = 0
        for o in self._hcf_fetch(to_fetch):
            self._cache[o['_key']] = o['value']
            count += 1
        self.logger.debug("Fetched %d items" % count)

    def flush(self, force_clear=False):
        buffer = []
        count = 0
        start = time()
        try:
            for fprint, state_val in six.iteritems(self._cache):
                buffer.append({'_key': fprint, 'value':state_val})
                if len(buffer) > 1024:
                    count += len(buffer)
                    self._store.set(buffer)
                    buffer = []
        finally:
            count += len(buffer)
            self._store.set(buffer)
        self.logger.debug("Send time %f ms", (time()-start) * 1000)
        self.logger.debug("State cache has been flushed: %d items" % count)
        super(HCFStates, self).flush(force_clear)


class HCFClientWrapper(object):

    def __init__(self, auth, project_id, frontier, batch_size=0, flush_interval=30):
        self._hs_client = HubstorageClient(auth=auth)
        self._hcf = self._hs_client.get_project(project_id).frontier
        self._hcf.batch_size = batch_size
        self._hcf.batch_interval = flush_interval
        self._frontier = frontier
        self._links_count = defaultdict(int)
        self._links_to_flush_count = defaultdict(int)
        self._hcf_retries = 10
        self.logger = logger.getChild(self.__class__.__name__)

    def add_request(self, slot, request):
        self._hcf.add(self._frontier, slot, [request])
        self._links_count[slot] += 1
        self._links_to_flush_count[slot] += 1
        return 0

    def flush(self, slot=None):
        n_links_to_flush = self.get_number_of_links_to_flush(slot)
        if n_links_to_flush:
            if slot is None:
                self._hcf.flush()
                for slot in self._links_to_flush_count.keys():
                    self._links_to_flush_count[slot] = 0
            else:
                writer = self._hcf._get_writer(self._frontier, slot)
                writer.flush()
                self._links_to_flush_count[slot] = 0
        return n_links_to_flush

    def read(self, slot, mincount=None):
        for i in range(self._hcf_retries):
            try:
                return self._hcf.read(self._frontier, slot, mincount)
            except requests_lib.exceptions.ReadTimeout:
                self.logger.error("Could not read from {0}/{1} try {2}/{3}".format(self._frontier, slot, i+1,
                                                                      self._hcf_retries))
            except requests_lib.exceptions.ConnectionError:
                self.logger.error("Connection error while reading from {0}/{1} try {2}/{3}".format(self._frontier, slot, i+1,
                                                                      self._hcf_retries))
            except requests_lib.exceptions.RequestException:
                self.logger.error("Error while reading from {0}/{1} try {2}/{3}".format(self._frontier, slot, i+1,
                                                                      self._hcf_retries))
            sleep(60 * (i + 1))
        return []

    def delete(self, slot, ids):
        for i in range(self._hcf_retries):
            try:
                self._hcf.delete(self._frontier, slot, ids)
                break
            except requests_lib.exceptions.ReadTimeout:
                self.logger.error("Could not delete ids from {0}/{1} try {2}/{3}".format(self._frontier, slot, i+1,
                                                                            self._hcf_retries))
            except requests_lib.exceptions.ConnectionError:
                self.logger.error("Connection error while deleting ids from {0}/{1} try {2}/{3}".format(self._frontier, slot, i+1,
                                                                            self._hcf_retries))
            except requests_lib.exceptions.RequestException:
                self.logger.error("Error deleting ids from {0}/{1} try {2}/{3}".format(self._frontier, slot, i+1,
                                                                            self._hcf_retries))
            sleep(60 * (i + 1))

    def delete_slot(self, slot):
        self._hcf.delete_slot(self._frontier, slot)

    def close(self):
        self._hcf.close()
        self._hs_client.close()

    def get_number_of_links(self, slot=None):
        if slot is None:
            return sum(self._links_count.values())
        else:
            return self._links_count[slot]

    def get_number_of_links_to_flush(self, slot=None):
        if slot is None:
            return sum(self._links_to_flush_count.values())
        else:
            return self._links_to_flush_count[slot]


class HCFQueue(Queue):
    def __init__(self, auth, project_id, frontier, batch_size, flush_interval, slots_count, slot_prefix,
                 cleanup_on_start, partitioner_cls):
        self.hcf = HCFClientWrapper(auth=auth,
                                    project_id=project_id,
                                    frontier=frontier,
                                    batch_size=batch_size,
                                    flush_interval=flush_interval)
        self.hcf_slots_count = slots_count
        self.hcf_slot_prefix = slot_prefix
        self.logger = logger.getChild(self.__class__.__name__)
        self.consumed_batches_ids = dict()
        self.partitions = [self.hcf_slot_prefix+str(i) for i in range(0, slots_count)]
        self.partitioner = partitioner_cls(self.partitions)

        if cleanup_on_start:
            for partition_id in self.partitions:
                self.hcf.delete_slot(partition_id)

    @classmethod
    def from_manager(cls, manager):
        s = manager.settings
        partitioner_cls = load_object(s.get('HCF_PARTITIONER_CLASS', 'frontera.contrib.backends.partitioners.FingerprintPartitioner'))
        return cls(s.get('HCF_AUTH', None),
                   s.get('HCF_PROJECT_ID'),
                   s.get('HCF_FRONTIER'),
                   s.get('HCF_PRODUCER_BATCH_SIZE', 10000),
                   s.get('HCF_PRODUCER_FLUSH_INTERVAL', 30),
                   s.get('HCF_PRODUCER_NUMBER_OF_SLOTS', 8),
                   s.get('HCF_PRODUCER_SLOT_PREFIX', ''),
                   s.get('HCF_CLEANUP_ON_START', False),
                   partitioner_cls)

    def frontier_start(self):
        pass

    def frontier_stop(self):
        self.hcf.close()

    def get_next_requests(self, max_next_requests, partition_id, **kwargs):
        return_requests = []
        data = True
        while data and len(return_requests) < max_next_requests:
            data = False
            consumed = []
            for batch in self.hcf.read(partition_id, max_next_requests):
                batch_id = batch['id']
                requests = batch['requests']
                data = len(requests) == max_next_requests
                self.logger.debug("got batch %s of size %d from HCF server" % (batch_id, len(requests)))
                for fingerprint, qdata in requests:
                    decoded = _convert_from_saved_type(qdata)
                    request = Request(decoded.get('url', fingerprint), **decoded['request'])
                    if request is not None:
                        request.meta.update({
                            'created_at': datetime.utcnow(),
                            'depth': 0,
                        })
                        request.meta.setdefault(b'scrapy_meta', {})
                        return_requests.append(request)
                consumed.append(batch_id)
            if consumed:
                self.hcf.delete(partition_id, consumed)
        return return_requests

    def schedule(self, batch):
        scheduled = 0
        for _, score, request, schedule in batch:
            if schedule:
                self._process_hcf_link(request, score)
                scheduled += 1
        self.logger.info('scheduled %d links' % scheduled)

    def _process_hcf_link(self, link, score):
        link_meta = getattr(link, 'meta', {})
        link_meta.pop(b'origin_is_frontier', None)
        scrapy_meta = link_meta.get(b'scrapy_meta', {})
        hcf_request = (
            link_meta.get('hcf_request')
            or scrapy_meta.get('hcf_request')
            or {}
        )
        hcf_request.setdefault('fp', link_meta.get('hcf_fingerprint', link.url))
        hcf_request.setdefault('p', link_meta.get(b'scrapy_priority', 0)),
        qdata = {'request': {
                    'method': link.method,
                    'headers': link.headers,
                    'cookies': link.cookies,
                    'meta': link.meta}
                }
        hcf_request['qdata'] = _convert_and_save_type(qdata)
        partition_id = self.partition_for(link)
        slot = self.hcf_slot_prefix + str(partition_id)
        self.hcf.add_request(slot, hcf_request)

    def partition_for(self, link):
        """Returns a partition ID for given link."""
        return self.partitioner.partition(link.meta[b'fingerprint'])

    def count(self):
        """
        Calculates lower estimate of items in the queue for all partitions.
        :return: int
        """
        count = 0
        for partition_id in self.partitions:
            for batch in self.hcf.read(partition_id):
                count += len(batch['requests'])
        return count


class HCFBackend(CommonBackend):

    name = 'HCF Backend'

    def __init__(self, manager, metadata, queue, states):
        settings = manager.settings
        self._metadata = metadata
        self._queue = queue
        self._states = states
        self.max_iterations = settings.get('HCF_CONSUMER_MAX_BATCHES', 0)
        self.consumer_slot = settings.get('HCF_CONSUMER_SLOT', 0)
        self.iteration = manager.iteration

    @classmethod
    def from_manager(cls, manager):
        s = manager.settings
        queue_cls = load_object(s.get('HCF_QUEUE_CLASS', 'hcf_backend.HCFQueue'))
        states_cls = load_object(s.get('HCF_STATES_CLASS', 'hcf_backend.HCFStates'))
        metadata = MemoryMetadata()
        queue = queue_cls.from_manager(manager)
        states = states_cls.from_manager(manager)
        return cls(manager, metadata, queue, states)

    @property
    def metadata(self):
        return self._metadata

    @property
    def queue(self):
        return self._queue

    @property
    def states(self):
        return self._states

    # TODO: we could collect errored pages, and schedule them back to HCF

    def finished(self):
        if self.max_iterations:
            return self.iteration > self.max_iterations
        return super(HCFBackend, self).finished()

    def get_next_requests(self, max_n_requests, **kwargs):
        batch = self.queue.get_next_requests(max_n_requests, self.consumer_slot, **kwargs)
        self.queue_size -= len(batch)
        return batch
