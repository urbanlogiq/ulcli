# Copyright (c), CommunityLogiq Software

"""Show the progress of downloads on a terminal."""

import contextlib
import math
import os
import threading
from collections import deque
from time import monotonic
from typing import Deque, Generator, Iterator, List, Self, TextIO, Tuple

REFRESH_SECONDS = 0.2
# Calculate the speed from this many seconds of downloads.
SPEED_SECONDS = 5
# Show no time left from this value up. The speed is too low to estimate it.
LONGEST_TIME_LEFT = 100 * 60 * 60

UNITS = ("B", "KiB", "MiB", "GiB", "TiB")


def format_size(size: float) -> str:
    """Return a number of bytes with a binary unit."""
    unit = 0
    # Change the unit before one decimal place shows 1024.0.
    while size >= 1023.95 and unit < len(UNITS) - 1:
        size /= 1024
        unit += 1
    return f"{size:.1f} {UNITS[unit]}" if unit else f"{int(size)} B"


def format_time(seconds: float) -> str:
    """Return a duration as h:mm:ss. Omit the hours if they are zero."""
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02}:{seconds:02}"
    return f"{minutes}:{seconds:02}"


def percent(part: int, whole: int) -> int:
    """Return `part` as a percentage of `whole`. An empty whole is complete."""
    return part * 100 // whole if whole else 100


def printable(text: str) -> str:
    """Replace the characters that a terminal does not show as text. Such a
    character can move the cursor."""
    return "".join(char if char.isprintable() else "?" for char in text)


def fit(text: str, columns: int) -> str:
    """Cut `text` to at most `columns` terminal columns.

    Count two columns for each character outside ASCII, because some are wide."""
    width = 0
    for index, char in enumerate(text):
        width += 1 if char.isascii() else 2
        if width > columns:
            return text[:index]
    return text


def is_terminal(stream: TextIO) -> bool:
    """Return True if `stream` is a terminal that can change its text in place."""
    return stream.isatty() and os.environ.get("TERM") != "dumb"


def terminal_size(stream: TextIO) -> os.terminal_size:
    """Return the size of the terminal, or 80 columns and 24 rows if it is
    unknown."""
    try:
        size = os.get_terminal_size(stream.fileno())
    except (OSError, ValueError):
        size = os.terminal_size((0, 0))
    if size.columns < 1 or size.lines < 1:
        return os.terminal_size((80, 24))
    return size


class Download:
    """One active download."""

    def __init__(self, name: str, size: int):
        self.name = name
        self.size = size
        self.received = 0


class Progress:
    """Show each active download and the total of all downloads on a terminal.

    Change the text in place. Show nothing if the stream is not a terminal or
    if there are no downloads."""

    def __init__(self, stream: TextIO, files: int, size: int):
        self._stream = stream
        self._files = files
        self._size = size

        # This lock protects the counts.
        self._lock = threading.Lock()
        self._active: List[Download] = []
        self._done = 0
        self._received = 0

        # This lock protects the terminal and the values below. Downloads do
        # not wait for it, so a slow terminal does not slow them down.
        self._terminal = threading.Lock()
        self._enabled = files > 0 and is_terminal(stream)
        self._shown = 0
        self._samples: Deque[Tuple[float, int]] = deque(
            maxlen=round(SPEED_SECONDS / REFRESH_SECONDS) + 1
        )
        self._speed = 0.0

        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> Self:
        if self._enabled:
            self._thread.start()
        return self

    def __exit__(self, *error: object) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()
        with self._terminal:
            self._enabled = False
            self._erase()

    def track(
        self, name: str, size: int, blocks: Generator[bytes, None, None]
    ) -> Generator[bytes, None, None]:
        """Yield the blocks of one download and count their bytes.

        Close `blocks` when the download ends or stops."""
        download = Download(printable(name), size)
        with self._lock:
            self._active.append(download)
        done = 0
        try:
            with contextlib.closing(blocks):
                for block in blocks:
                    with self._lock:
                        download.received += len(block)
                        self._received += len(block)
                    yield block
                    # Release this block before the next read.
                    del block
            done = 1
        finally:
            with self._lock:
                self._active.remove(download)
                self._done += done

    @contextlib.contextmanager
    def hidden(self) -> Iterator[None]:
        """Remove the display while the caller writes to the terminal."""
        with self._terminal:
            self._erase()
            try:
                yield
            finally:
                self._draw()

    def refresh(self) -> None:
        """Measure the speed and show the current state."""
        with self._terminal:
            with self._lock:
                received = self._received
            self._samples.append((monotonic(), received))
            (start, before), (end, after) = self._samples[0], self._samples[-1]
            self._speed = (after - before) / (end - start) if end > start else 0.0
            self._draw()

    def _run(self) -> None:
        while not self._stop.wait(REFRESH_SECONDS):
            self.refresh()

    def _lines(self, capacity: int) -> List[str]:
        """Return the display in at most `capacity` lines."""
        with self._lock:
            done, received = self._done, self._received
            active = [(file.name, file.received, file.size) for file in self._active]

        total = (
            f"Downloaded {done} of {self._files} files, {format_size(received)} of"
            f" {format_size(self._size)} ({percent(received, self._size)}%),"
            f" {format_size(self._speed)}/s"
        )
        if self._speed > 0:
            left = (self._size - received) / self._speed
            if 0 <= left < LONGEST_TIME_LEFT:
                # Show zero only when the downloads are complete.
                total += f", {format_time(math.ceil(left))} left"

        # If the downloads do not all fit, the last line gives the number of
        # downloads that the display omits.
        room = capacity - 1
        shown = active if len(active) <= room else active[: max(room - 1, 0)]
        lines = [total] + [
            f"{percent(count, size):>4}%  {format_size(count):>10} of"
            f" {format_size(size):<10}  {name}"
            for name, count, size in shown
        ]
        if len(shown) < len(active):
            lines.append(f"  and {len(active) - len(shown)} more")
        return lines[:capacity]

    def _draw(self) -> None:
        """Replace the display with the current state."""
        if not self._enabled:
            return
        columns, rows = terminal_size(self._stream)
        # Keep the last column free, so that no line continues on the next
        # row. Keep one row for the cursor below the display.
        lines = [fit(line, columns - 1) for line in self._lines(rows - 1)]
        text = "".join(f"{line}\x1b[K\n" for line in lines)
        self._write(f"{self._top()}{text}\x1b[J")
        self._shown = len(lines)

    def _erase(self) -> None:
        """Remove the display from the terminal."""
        if self._shown:
            self._write(f"{self._top()}\x1b[J")
            self._shown = 0

    def _top(self) -> str:
        """Return the text that moves the cursor to the start of the display."""
        return f"\r\x1b[{self._shown}A" if self._shown else "\r"

    def _write(self, text: str) -> None:
        # A terminal that fails must not stop the downloads.
        with contextlib.suppress(OSError, ValueError):
            self._stream.write(text)
            self._stream.flush()
