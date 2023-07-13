"""
taskmaster.controller
~~~~~~~~~~~~~~~~~~~~~

:copyright: (c) 2010 DISQUS.
:license: Apache License 2.0, see LICENSE for more details.
"""

import hashlib
import sys
from os import path, rename, unlink

import pickle
import gevent
import zmq.green as zmq
from gevent.queue import Empty, Queue

from taskmaster.constants import DEFAULT_ITERATOR_TARGET, DEFAULT_LOG_LEVEL
from taskmaster.util import get_logger, import_target


class Server(object):
    def __init__(self, address, size=None, log_level=DEFAULT_LOG_LEVEL):
        self.daemon = True
        self.started = False
        self.size = size
        self.queue = Queue(maxsize=size)
        self.address = address

        self.context = zmq.Context(1)
        self.server = None
        self.logger = get_logger(self, log_level)

        self._has_fetched_jobs = False

    def send(self, cmd: bytes, data: bytes=b""):
        self.server.send_multipart([cmd, data])

    def recv(self):
        reply = self.server.recv_multipart()

        assert len(reply) == 2

        return reply

    def bind(self):
        if self.server:
            self.server.close()

        self.server = self.context.socket(zmq.REP)
        self.server.bind(self.address)

    def start(self):
        self.started = True

        self.logger.info("Taskmaster binding to %r", self.address)
        self.bind()

        while self.started:
            gevent.sleep(0)
            cmd, data = self.recv()
            if cmd == b"GET":
                if not self.has_work():
                    self.send(b"QUIT")
                    continue

                try:
                    job = self.queue.get_nowait()
                except Empty:
                    self.send(b"WAIT")
                    continue

                self.send(b"OK", pickle.dumps(job))

            elif cmd == b"DONE":
                self.queue.task_done()
                if self.has_work():
                    self.send(b"OK")
                else:
                    self.send(b"QUIT")

            else:
                self.send(b"ERROR", "Unrecognized command")

        self.logger.info("Shutting down")
        self.shutdown()

    def mark_queue_filled(self):
        self._has_fetched_jobs = True

    def put_job(self, job):
        return self.queue.put(job)

    def first_job(self):
        return self.queue.queue[0]

    def get_current_size(self):
        return self.queue.qsize()

    def get_max_size(self):
        return self.size

    def has_work(self):
        if not self._has_fetched_jobs:
            return True
        return not self.queue.empty()

    def is_alive(self):
        return self.started

    def shutdown(self):
        if not self.started:
            return
        self.server.close()
        self.context.term()
        self.started = False


class Controller(object):
    def __init__(self, server, target, kwargs=None, state_file=None, progressbar=True, log_level=DEFAULT_LOG_LEVEL):
        if isinstance(target, str):
            target = import_target(target, DEFAULT_ITERATOR_TARGET)

        if not state_file:
            target_file = sys.modules[target.__module__].__file__.rsplit(".", 1)[0]
            state_file = path.join(path.dirname(target_file), "%s" % (path.basename(target_file),))
            if kwargs:
                checksum = hashlib.md5()
                for k, v in sorted(kwargs.items()):
                    checksum.update("%s=%s" % (k, v))
                state_file += ".%s" % checksum.hexdigest()
            state_file += ".state"
            print(state_file)

        self.server = server
        self.target = target
        self.target_kwargs = kwargs
        self.state_file = state_file
        if progressbar:
            self.pbar = self.get_progressbar()
        else:
            self.pbar = None
        self.logger = get_logger(self, log_level)

    def get_progressbar(self):
        from taskmaster.progressbar import Counter, ProgressBar, Speed, Timer, UnknownLength, Value

        sizelen = len(str(self.server.size))
        format = "In-Queue: %%-%ds / %%-%ds" % (sizelen, sizelen)

        queue_size = Value(callback=lambda x: format % (self.server.get_current_size(), self.server.get_max_size()))

        widgets = ["Completed Tasks: ", Counter(), " | ", queue_size, " | ", Speed(), " | ", Timer()]

        pbar = ProgressBar(widgets=widgets, maxval=UnknownLength)

        return pbar

    def read_state(self):
        if path.exists(self.state_file):
            self.logger.info("Reading previous state from %r", self.state_file)
            with open(self.state_file, "rb") as fp:
                try:
                    return pickle.load(fp)
                except EOFError:
                    pass
                except Exception as e:
                    self.logger.exception(
                        "There was an error reading from state file. Ignoring and continuing without.\n%s", e
                    )
        return {}

    def update_state(self, job_id, job, fp=None):
        last_job_id = getattr(self, "_last_job_id", None)

        if self.pbar:
            self.pbar.update(job_id)

        if job_id == last_job_id:
            return

        if not job:
            return

        last_job_id = job_id

        data = {
            "job": job,
            "job_id": job_id,
        }

        with open(self.state_file + ".tmp", "wb") as fp:
            pickle.dump(data, fp)
        rename(self.state_file + ".tmp", self.state_file)

    def state_writer(self):
        while self.server.is_alive():
            # state is not guaranteed accurate, as we do not
            # update the file on every iteration
            gevent.sleep(0.01)

            try:
                job_id, job = self.server.first_job()
            except IndexError:
                self.update_state(None, None)
                continue

            self.update_state(job_id, job)

    def reset(self):
        if path.exists(self.state_file):
            unlink(self.state_file)

    def start(self):
        if self.target_kwargs:
            kwargs = self.target_kwargs.copy()
        else:
            kwargs = {}

        last_job = self.read_state()
        if last_job:
            kwargs["last"] = last_job["job"]
            start_id = last_job["job_id"]
        else:
            start_id = 0

        gevent.spawn(self.server.start)

        # context switch so the server can spawn
        gevent.sleep(0)

        if self.pbar:
            self.pbar.start()
            self.pbar.update(start_id)

        state_writer = gevent.spawn(self.state_writer)

        job_id, job = (None, None)
        for job_id, job in enumerate(self.target(**kwargs), start_id):
            self.server.put_job((job_id, job))
            gevent.sleep(0)
        self.server.mark_queue_filled()

        while self.server.has_work():
            gevent.sleep(0.01)

        # Give clients a few seconds to receive a DONE message
        gevent.sleep(3)

        self.server.shutdown()
        state_writer.join(1)

        self.update_state(job_id, job)

        if self.pbar:
            self.pbar.finish()
