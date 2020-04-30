#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncclick as click
import asyncio
import logging
import sys

from enum import Enum
from typing import Callable, List, Optional


def set_up_logger(name: str, level: int =logging.INFO) -> logging.Logger:
    """
    Set up logger with timestamp.

    Args:
        name: Name of logger
        level: Logging level, dafaults to INFO

    Returns:
        Logger instance

    """
    formatter = logging.Formatter(
        fmt='%(asctime)s %(levelname)-8s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.addHandler(handler)
    return logger


LOGGER = set_up_logger('ncwatch')
POLL = 'poll'


class Events(Enum):
    """
    Events emitted by fswatch to pay attention to.
    """
    Created = 0
    Updated = 1
    Removed = 2
    Renamed = 3
    OwnerModified = 4
    AttributeModified = 5
    MovedFrom = 6
    MovedTo = 7


class Watcher:
    """
    File Watcher class. Asynchronously using fswatch to monitor given files and
    directories for changes.
    """
    def __init__(self, queue: asyncio.Queue):
        """
        Watcher class constructor. Setting up watchers, subprocess manager and
        task queue.

        Args:
            queue: Queue to add events to

        """
        self._watchers = set()
        self._process: Optional[asyncio.subprocess.Process] = None
        self._queue = queue

    async def _read_stream(self, stream: asyncio.streams.StreamReader):
        """
        Read stream from subprocess stdout. Add new line to the task queue as
        long as the line is not empty and the queue is not full.

        Args:
            stream: Stream reader

        """
        while True:
            line = await stream.readline()
            line = line.decode().strip('\n')
            if not line.strip():
                continue
            if self._queue.full():
                continue
            await self._queue.put(line)

    async def start(self, consumer: Callable, recursive: bool = False,
                    batch: bool = False, exclude: Optional[List[str]] = None):
        """
        Start file watcher.

        Args:
            consumer: Function consuming watch events
            recursive: Also watch sub directories of the given directory
            batch: Batch concurrent fswatch events together
            exclude: Exclude files matching any of the following regexes

        """
        args = list(self._watchers)
        exclude = exclude or []

        # Filter events
        for event in Events:
            args = ['--event', event.name] + args

        # Add exclusions
        for regex in exclude:
            args = ['-e', regex] + args
        args.insert(0, '-E')

        # Add latency
        args = ['-l', '2'] + args

        # Set recursiveness
        if recursive:
            args.insert(0, '-r')

        # Set batch mode
        if batch:
            args.insert(0, '-o')

        self._process = await asyncio.create_subprocess_exec(
            'fswatch',
            *args,
            stdout=asyncio.subprocess.PIPE
        )

        LOGGER.info('Listening...')
        LOGGER.debug(f'fswatch {" ".join(args)}')
        await asyncio.gather(
            consumer(),
            self._read_stream(self._process.stdout),
        )

    async def stop(self):
        """
        Stop file watcher.
        """
        if self._process is None:
            return
        await self._process.terminate()

    def watch(self, path: str):
        """
        Watch the given directory or file. The watcher needs restarting for the
        change to take effect.

        Args:
            path: Path to file or directory to watch

        """
        self._watchers.add(path)

    def unwatch(self, path: str):
        """
        Stop watching the given directory or file. The watcher needs restarting
        for the change to take effect.

        Args:
            path: Path to file or directory to stop watching

        Raises:
            KeyError: When not watching the given path

        """
        self._watchers.remove(path)


class NextcloudSync:
    """
    Nextcloud Sync Manager.
    """

    def __init__(self, sourcedir: str, nextcloudurl: str, queue: asyncio.Queue):
        """
        NextcloudSync class constructor. Reads source dir to be synchronised and
        Nextcloud url to synchronise with.

        Args:
            sourcedir: Local directory to sync
            nextcloudurl: Nextcloud server to sync with
            queue: Queue that triggers sync

        """
        self._sourcedir = sourcedir
        self._nextcloudurl = nextcloudurl
        self._queue = queue

    async def poll(self, interval: int):
        """
        Trigger sync tool at an interval in order to poll the server for new
        changes.

        Args:
            interval: Poll interval in seconds

        """
        while True:
            await asyncio.sleep(interval)
            if self._queue.full():
                continue
            await self._queue.put(POLL)

    async def consume(self):
        """
        Trigger Nextcloud sync when new items are added to the given task queue.
        """
        while True:
            line = await self._queue.get()
            if line == POLL:
                LOGGER.info('Polling server')
            else:
                LOGGER.info('File system change detected, starting sync')

            process = await asyncio.create_subprocess_exec(
                'nextcloudcmd',
                self._sourcedir,
                self._nextcloudurl,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            if process.returncode:
                LOGGER.critical(stderr.decode().strip())
            else:
                LOGGER.info('Done!')


@click.command()
@click.argument(
    'sourcedir',
    envvar='SOURCEDIR',
    required=True,
)
@click.argument(
    'nextcloudurl',
    envvar='NEXTCLOUDURL',
    required=True,
)
@click.option(
    '-r',
    '--recursive',
    is_flag=True,
    default=True,
    show_default=True,
    help='Sync sub directories'
)
@click.option(
    '-p',
    '--poll-interval',
    type=int,
    default=30,
    show_default=True,
    help='Seconds between polling server for changes'
)
async def main(sourcedir: str, nextcloudurl: str, recursive: bool,
               poll_interval: int):
    """
    File watcher trigger for the "nextcloudcmd" CLI client
    """
    queue = asyncio.Queue(maxsize=1)
    syncer = NextcloudSync(sourcedir, nextcloudurl, queue)
    watcher = Watcher(queue)
    watcher.watch(sourcedir)
    await asyncio.gather(
        watcher.start(
            consumer=syncer.consume,
            recursive=recursive,
            batch=False,
            exclude=[r'\._sync_[a-zA-Z0-9]+\.db']
        ),
        syncer.poll(
            interval=poll_interval
        )
    )


if __name__ == '__main__':
    try:
        main(_anyio_backend='asyncio', auto_envvar_prefix='NC')
    except KeyboardInterrupt:
        LOGGER.info('Exiting...')
