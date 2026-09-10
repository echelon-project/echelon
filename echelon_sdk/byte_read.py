"""byte_read.py — byte-level windowed file reading (no line-based I/O).

Reads arbitrary byte ranges from a file using raw os file operations, never
loading the whole file into memory. Handles large files, edge offsets, and
provides smart head+tail peeking for orientation — the byte-level analogue of
the line-based smart default in tools_fileops._read_file.

Owner, 2026-06-07: the agent's file-reading layer needed a byte-oriented sibling
for binary files, oversized logs, and any case where line-splitting is wasteful
or wrong. This module is free functions (no mixin) — a utility, not a tool.
"""
from __future__ import annotations

import os
from typing import IO


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SMALL_BYTES = 32_768   # 32 KiB — files under this are read whole
WINDOW = 16_384        # 16 KiB default window
PEEK = 4_096           # 4 KiB head/tail for smart default


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def read_window(
    path: str | os.PathLike,
    offset: int | None = None,
    limit: int | None = None,
    window: int = WINDOW,
    peek: int = PEEK,
) -> str:
    """Read a byte-range window from *path* without loading the whole file.

    Parameters
    ----------
    path
        Path to the file (str or PathLike).
    offset
        Byte offset to start reading (0-based).  ``None``  means start at 0.
    limit
        Maximum number of bytes to read.  ``None``  means read to end-of-file
        (or use the smart default when *offset* is also ``None``).
    window
        Default chunk size (bytes) when *limit* is not given and *offset* is
        set.  Default 16 384 (16 KiB).
    peek
        Number of bytes to show from head **and** tail when neither *offset*
        nor *limit* is given (smart orientation for large files).  Default
        4 096 (4 KiB).

    Returns
    -------
    str
        A human-readable report containing the byte range read, the file size,
        and the raw bytes decoded as UTF-8 (with replacement for non-UTF-8
        sequences).  For large files with no explicit range, shows head + tail
        with a gap marker.

    Raises
    ------
    FileNotFoundError
        If *path* does not exist.
    IsADirectoryError
        If *path* is a directory.
    OSError
        On other OS-level I/O errors.
    """
    path = os.fspath(path)

    # --- stat the file first (cheap, no read) ---
    stat = os.stat(path)
    size = stat.st_size

    # --- resolve the byte range ---
    if offset is None and limit is None:
        # Smart default: small file → whole; big file → head + tail
        if size <= SMALL_BYTES:
            data = _read_exact(path, 0, size)
            return _fmt_whole(size, data)
        return _fmt_head_tail(path, size, peek)

    off = offset if offset is not None else 0
    lim = limit if limit is not None else window

    # Clamp to file bounds
    if off < 0:
        off = max(0, size + off)   # negative offset = from end
    if off >= size:
        return f"[file size {size} bytes — offset {off} is past end, nothing to read]"

    count = min(lim, size - off)
    data = _read_exact(path, off, count)
    return _fmt_window(off, count, size, data)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_exact(path: str, offset: int, count: int) -> bytes:
    """Read exactly *count* bytes from *path* starting at *offset*.

    Uses ``os.open`` / ``os.read`` in a loop to guarantee the full range is
    read (handles short reads on pipes/slow storage, though for regular files
    this is belt-and-suspenders).
    """
    fd: int | None = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_BINARY if hasattr(os, "O_BINARY") else os.O_RDONLY)
        os.lseek(fd, offset, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = count
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break   # EOF before expected count (shouldn't happen for regular files)
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        if fd is not None:
            os.close(fd)


def _fmt_whole(size: int, data: bytes) -> str:
    """Format a whole-file read."""
    decoded = data.decode("utf-8", errors="replace")
    return f"[whole file, {size} bytes]\n{decoded}"


def _fmt_head_tail(path: str, size: int, peek: int) -> str:
    """Format a head+tail peek for large files."""
    head_count = min(peek, size)
    tail_count = min(peek, size)
    head = _read_exact(path, 0, head_count)
    tail_start = max(0, size - tail_count)
    tail = _read_exact(path, tail_start, tail_count)

    head_decoded = head.decode("utf-8", errors="replace")
    tail_decoded = tail.decode("utf-8", errors="replace")

    gap = size - head_count - tail_count
    if gap < 0:
        # head and tail overlap — just show the whole thing
        return _fmt_whole(size, head[:size])

    return (
        f"[{size} bytes total — showing HEAD 0-{head_count} and TAIL "
        f"{tail_start}-{size}.  Page the middle with read_window(offset=<byte>, "
        f"limit={WINDOW}); reading the window(s) you'll edit satisfies the "
        f"read-before-edit guard.]\n"
        f"--- HEAD (0-{head_count}) ---\n{head_decoded}\n"
        f"...[{gap} bytes not shown — page them]...\n"
        f"--- TAIL ({tail_start}-{size}) ---\n{tail_decoded}"
    )


def _fmt_window(offset: int, count: int, size: int, data: bytes) -> str:
    """Format a windowed byte-range read."""
    decoded = data.decode("utf-8", errors="replace")
    end = offset + count
    more = "" if end >= size else \
        f"\n...[paged window; next: read_window(offset={end}, limit={WINDOW})]"
    return f"[bytes {offset}-{end} of {size}]\n{decoded}{more}"
