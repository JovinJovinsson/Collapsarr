"""Shared zip-slip / tar-slip (path-traversal) entry-name guard.

Extracted from :mod:`collapsarr.restore.upload` (COL-73, the original
``_is_unsafe_zip_entry``) so this codebase's two archive-extraction call
sites -- the untrusted-upload restore path (:mod:`collapsarr.restore.upload`)
and the checksum-verified FFmpeg auto-download path
(:mod:`collapsarr.ffmpeg_download.service`, COL-222) -- share one
implementation of a security-sensitive check rather than two independently-
maintained copies that could silently drift apart (e.g. a new traversal shape
discovered and patched into one but not the other).

The check itself is archive-format-agnostic: it operates on a plain entry
name string, so it applies unchanged to a :class:`zipfile.ZipInfo.filename`
or a :class:`tarfile.TarInfo.name` alike -- both archive formats can carry
the identical traversal shapes.
"""

from __future__ import annotations

import ntpath

__all__ = ["is_unsafe_archive_entry_path"]


def is_unsafe_archive_entry_path(name: str) -> bool:
    """Return whether an archive entry name is a zip-slip / path-traversal attempt.

    An entry is unsafe if it is an absolute path (POSIX ``/`` or Windows
    ``\\``), carries a drive letter or UNC prefix, or contains a ``..``
    component under either separator -- any of which, under a naive
    ``extractall``, could write outside the extraction root. Both separators
    are handled because an archive authored on Windows may use backslashes.
    An empty name is treated as safe (not itself a traversal attempt) --
    callers extracting a specific named member already won't match one
    against an expected filename.
    """
    if not name:
        return False
    if name.startswith("/") or name.startswith("\\"):
        return True
    if ntpath.splitdrive(name)[0]:
        return True
    parts = name.replace("\\", "/").split("/")
    return ".." in parts
