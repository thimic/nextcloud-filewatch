#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncclick as click
import asyncio
import logging
import psutil
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


def has_handle(file_path: str) -> bool:
    """
    Check if a file is open in another process.

    Args:
        file_path: File path to check

    Returns:
        True if file is open, else False

    """
    for proc in psutil.process_iter():
        try:
            for item in proc.open_files():
                if file_path == item.path:
                    return True
        except Exception:
            LOGGER.exception('Error while checking file handle:')
    return False


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
        self._cancelled = False

    async def _read_stream(self, stream: asyncio.streams.StreamReader):
        """
        Read stream from subprocess stdout. Add new line to the task queue as
        long as the line is not empty and the queue is not full.

        Args:
            stream: Stream reader

        """
        while True:
            line = await stream.readline()
            if self._queue.full():
                continue
            line = line.decode().strip('\n')
            if not line.strip():
                continue

            await self._queue.put(line)

    async def start(self, recursive: bool = False,
                    batch: bool = False, exclude: Optional[List[str]] = None):
        """
        Start file watcher.

        Args:
            recursive: Also watch sub directories of the given directory
            batch: Batch concurrent fswatch events together
            exclude: Exclude files matching any of the following regexes

        """
        self._cancelled = False
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

        while not self._cancelled:
            self._process = await asyncio.create_subprocess_exec(
                'fswatch',
                *args,
                stdout=asyncio.subprocess.PIPE
            )
            LOGGER.info('Listening...')
            LOGGER.debug(f'fswatch {" ".join(args)}')
            await asyncio.gather(
                self._read_stream(self._process.stdout),
            )
            stdout, stderr = await self._process.communicate()
            if self._process.returncode:
                LOGGER.critical(stderr.decode().strip())
                LOGGER.info('Restarting fswatch...')
            else:
                return

    async def stop(self):
        """
        Stop file watcher.
        """
        self._cancelled = True
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
        self._files = {}
        self._cancelled = False

    def stop(self):
        """
        Stop Sync manager.
        """
        self._cancelled = True

    async def poll(self, interval: int):
        """
        Trigger sync tool at an interval in order to poll the server for new
        changes.

        Args:
            interval: Poll interval in seconds

        """
        self._cancelled = False
        while not self._cancelled:
            await asyncio.sleep(interval)
            if self._queue.full():
                continue
            await self._queue.put(POLL)

    async def consume(self):
        """
        Trigger Nextcloud sync when new items are added to the given task queue.
        """
        while not self._cancelled:
            line = await self._queue.get()
            if line == POLL:
                LOGGER.info('Polling server')
            else:
                LOGGER.info('File system change detected, starting sync')
            LOGGER.info(f'Change: {line}')

            if line != POLL:
                while has_handle(line):
                    LOGGER.info(f'File {line!r} is in use, waiting...')
                    await asyncio.sleep(1)

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
                LOGGER.info(f'Done: {line}')


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
            recursive=recursive,
            batch=False,
            exclude=[r'\._sync_[a-zA-Z0-9]+\.db']
        ),
        syncer.poll(
            interval=poll_interval
        ),
        syncer.consume()
    )


if __name__ == '__main__':
    try:
        main(_anyio_backend='asyncio', auto_envvar_prefix='NC')
    except KeyboardInterrupt:
        LOGGER.info('Exiting...')
