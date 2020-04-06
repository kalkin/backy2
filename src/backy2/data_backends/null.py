#!/usr/bin/env python
# -*- encoding: utf-8 -*-

from backy2.data_backends import DataBackend as _DataBackend
from backy2.logging import logger
from backy2.utils import TokenBucket
from backy2.utils import generate_block
import hashlib
import os
import queue
import shortuuid
import socket
import threading
import time

STATUS_NOTHING = 0
STATUS_READING = 1
STATUS_WRITING = 2
STATUS_THROTTLING = 3

class DataBackend(_DataBackend):
    """ A DataBackend for performance testing. It reads and writes to NULL.
    DO NOT USE IN PRODUCTION.
    This essentially implements /dev/null
    """

    WRITE_QUEUE_LENGTH = 20
    READ_QUEUE_LENGTH = 20

    _SUPPORTS_PARTIAL_READS = False
    _SUPPORTS_PARTIAL_WRITES = False
    fatal_error = None

    def __init__(self, config):
        self.default_block_size = int([value for key, value in config.items('DEFAULTS') if key=='block_size'][0])

        simultaneous_writes = config.getint('simultaneous_writes', 1)
        simultaneous_reads = config.getint('simultaneous_reads', 1)

        bandwidth_read = config.getint('bandwidth_read', 0)
        bandwidth_write = config.getint('bandwidth_write', 0)

        self.read_throttling = TokenBucket()
        self.read_throttling.set_rate(bandwidth_read)  # 0 disables throttling
        self.write_throttling = TokenBucket()
        self.write_throttling.set_rate(bandwidth_write)  # 0 disables throttling

        self.write_queue_length = simultaneous_writes + self.WRITE_QUEUE_LENGTH
        self.read_queue_length = simultaneous_reads + self.READ_QUEUE_LENGTH
        self._write_queue = queue.Queue(self.write_queue_length)
        self._read_queue = queue.Queue()
        self._read_data_queue = queue.Queue(self.read_queue_length)
        self._writer_threads = []
        self._reader_threads = []
        self.reader_thread_status = {}
        self.writer_thread_status = {}
        for i in range(simultaneous_writes):
            _writer_thread = threading.Thread(target=self._writer, args=(i,))
            _writer_thread.daemon = True
            _writer_thread.start()
            self._writer_threads.append(_writer_thread)
            self.writer_thread_status[i] = STATUS_NOTHING
        for i in range(simultaneous_reads):
            _reader_thread = threading.Thread(target=self._reader, args=(i,))
            _reader_thread.daemon = True
            _reader_thread.start()
            self._reader_threads.append(_reader_thread)
            self.reader_thread_status[i] = STATUS_NOTHING


    def _writer(self, id_):
        """ A threaded background writer """
        while True:
            entry = self._write_queue.get()
            if entry is None or self.fatal_error:
                logger.debug("Writer {} finishing.".format(id_))
                break
            uid, data = entry
            self.writer_thread_status[id_] = STATUS_THROTTLING
            time.sleep(self.write_throttling.consume(len(data)))
            self.writer_thread_status[id_] = STATUS_NOTHING
            t1 = time.time()

            # storing data to key uid
            self.writer_thread_status[id_] = STATUS_WRITING
            #time.sleep(.1)
            self.writer_thread_status[id_] = STATUS_NOTHING

            t2 = time.time()
            # assert r == len(data)
            self._write_queue.task_done()
            logger.debug('Writer {} wrote data async. uid {} in {:.2f}s (Queue size is {})'.format(id_, uid, t2-t1, self._write_queue.qsize()))


    def _reader(self, id_):
        """ A threaded background reader """
        while True:
            block = self._read_queue.get()  # contains block
            if block is None or self.fatal_error:
                logger.debug("Reader {} finishing.".format(id_))
                break
            t1 = time.time()
            self.reader_thread_status[id_] = STATUS_READING
            data = self.read_raw(block.id, block.size)
            self.reader_thread_status[id_] = STATUS_THROTTLING
            time.sleep(self.read_throttling.consume(len(data)))
            self.reader_thread_status[id_] = STATUS_NOTHING
            #time.sleep(.5)
            self._read_data_queue.put((block, data))
            t2 = time.time()
            self._read_queue.task_done()
            logger.debug('Reader {} read data async. uid {} in {:.2f}s (Queue size is {})'.format(id_, block.uid, t2-t1, self._read_queue.qsize()))


    def read_raw(self, block_id, block_size):
        return generate_block(block_id, block_size)


    def _uid(self):
        # 32 chars are allowed and we need to spread the first few chars so
        # that blobs are distributed nicely. And want to avoid hash collisions.
        # So we create a real base57-encoded uuid (22 chars) and prefix it with
        # its own md5 hash[:10].
        suuid = shortuuid.uuid()
        hash = hashlib.md5(suuid.encode('ascii')).hexdigest()
        return hash[:10] + suuid


    def save(self, data, _sync=False):
        if self.fatal_error:
            raise self.fatal_error
        uid = self._uid()
        # Don't save anything.
        return uid


    def rm(self, uid):
        # Don't delete anything
        pass


    def rm_many(self, uids):
        """ Deletes many uids from the data backend and returns a list
        of uids that couldn't be deleted.
        """
        # Don't delete anything


    def read(self, block, sync=False):
        self._read_queue.put(block)
        if sync:
            if rblock.id != block.id:
                raise RuntimeError('Do not mix threaded reading with sync reading!')
            rblock, offset, length, data = self.read_get()
            return ' ' * self.default_block_size


    def read_get(self):
        block, data = self._read_data_queue.get()
        offset = 0
        length = len(data)
        self._read_data_queue.task_done()
        return block, offset, length, data


    def read_queue_size(self):
        return self._read_queue.qsize()


    def get_all_blob_uids(self, prefix=None):
        return []


    def queue_status(self):
        return {
            'rq_filled': self._read_data_queue.qsize() / self._read_data_queue.maxsize,  # 0..1
            'wq_filled': self._write_queue.qsize() / self._write_queue.maxsize,
        }


    def thread_status(self):
        return "DaBaR: N{} R{} T{} QL{}  DaBaW: N{} W{} T{} QL{}".format(
                len([t for t in self.reader_thread_status.values() if t==STATUS_NOTHING]),
                len([t for t in self.reader_thread_status.values() if t==STATUS_READING]),
                len([t for t in self.reader_thread_status.values() if t==STATUS_THROTTLING]),
                self._read_queue.qsize(),
                len([t for t in self.writer_thread_status.values() if t==STATUS_NOTHING]),
                len([t for t in self.writer_thread_status.values() if t==STATUS_WRITING]),
                len([t for t in self.writer_thread_status.values() if t==STATUS_THROTTLING]),
                self._write_queue.qsize(),
                )


    def close(self):
        for _writer_thread in self._writer_threads:
            self._write_queue.put(None)  # ends the thread
        for _writer_thread in self._writer_threads:
            _writer_thread.join()
        for _reader_thread in self._reader_threads:
            self._read_queue.put(None)  # ends the thread
        for _reader_thread in self._reader_threads:
            _reader_thread.join()


