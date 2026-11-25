"""Read a zip archive a publisher sent (a .zip, or the zip inside every .xlsx) without trusting it.

The archive's own bytes are what the raw store keeps; this reads the one part asked for, and refuses rather than
guesses when the archive could be read two ways or could expand without bound:

- at most ``max_members`` entries; two entries with the same name (after normalising) refused, since readers
  disagree on which one wins;
- an entry that is encrypted, a symbolic link, an absolute path or one that climbs out with ``..`` refused, for
  the whole archive, not skipped;
- the declared uncompressed size of every entry, in total, at most ``max_uncompressed_bytes``, and no entry's
  declared ratio (uncompressed over compressed) past ``max_ratio``; the bytes actually read are capped again, so a
  header that lies about its size gains nothing;
- the member read must not itself be an archive (no nested zips).
"""

from __future__ import annotations

import io
import posixpath
import stat
import zipfile
from dataclasses import dataclass

from health_data_registry.adapters import LayoutError

ZIP_MAGIC = b"PK\x03\x04"
CHUNK = 64 * 1024


class ArchiveRefused(LayoutError):
    """The archive can't be read safely or unambiguously; nothing was read from it."""


@dataclass(frozen=True)
class Caps:
    max_uncompressed_bytes: int
    max_ratio: int
    max_members: int


def _clean(name: str) -> str:
    return posixpath.normpath(name.replace("\\", "/"))


class SafeZip:
    """A checked zip archive: every entry was inspected before any part is read."""

    def __init__(self, content: bytes, caps: Caps, *, what: str = "the archive") -> None:
        self.caps, self.what = caps, what
        try:
            self.zf = zipfile.ZipFile(io.BytesIO(content))
            infos = self.zf.infolist()
        except (zipfile.BadZipFile, OSError, ValueError) as e:
            raise ArchiveRefused(f"{what} is not a readable zip archive") from e
        if len(infos) > caps.max_members:
            raise ArchiveRefused(f"{what} has {len(infos)} entries, more than {caps.max_members}")
        names: dict[str, zipfile.ZipInfo] = {}
        total = 0
        for info in infos:
            raw = info.filename
            if (
                raw.startswith(("/", "\\"))
                or (len(raw) > 1 and raw[1] == ":")
                or ".." in raw.replace("\\", "/").split("/")
            ):
                raise ArchiveRefused(f"{what} has an entry outside the archive ({raw!r})")
            if info.flag_bits & 0x1:
                raise ArchiveRefused(f"{what} has an encrypted entry ({raw!r})")
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ArchiveRefused(f"{what} has a symbolic link ({raw!r})")
            key = _clean(raw)
            if key in names:
                raise ArchiveRefused(f"{what} has two entries named {key!r}")
            names[key] = info
            total += info.file_size
            if info.file_size > caps.max_ratio * max(info.compress_size, 1):
                raise ArchiveRefused(f"{what}: {raw!r} expands more than {caps.max_ratio} times")
        if total > caps.max_uncompressed_bytes:
            raise ArchiveRefused(
                f"{what} expands to {total:,} bytes, more than {caps.max_uncompressed_bytes:,}"
            )
        self.names = names
        self.inflated = 0  # bytes read out of the archive so far, across every part: capped as a whole

    def has(self, name: str) -> bool:
        return _clean(name) in self.names

    def read(self, name: str) -> bytes:
        """One entry's bytes, read with a cap on what is actually inflated (the header's sizes aren't trusted)."""
        info = self.names.get(_clean(name))
        if info is None:
            raise ArchiveRefused(f"{self.what} has no entry {name!r}")
        limit = min(
            self.caps.max_uncompressed_bytes - self.inflated, self.caps.max_ratio * max(info.compress_size, 1)
        )
        out = bytearray()
        try:
            with self.zf.open(info) as fh:
                while chunk := fh.read(CHUNK):
                    out += chunk
                    if len(out) > limit:
                        raise ArchiveRefused(f"{self.what}: {name!r} expands past its cap")
        except (zipfile.BadZipFile, OSError, EOFError, RuntimeError) as e:
            raise ArchiveRefused(f"{self.what}: {name!r} can't be read") from e
        self.inflated += len(out)
        return bytes(out)


def read_member(content: bytes, member: str, caps: Caps, *, container: bool = False) -> bytes:
    """The one named member of a zip archive. A member that is itself an archive is refused, unless the format
    read is a container (an xlsx file is a zip of XML parts, opened again through SafeZip with the same caps)."""
    data = SafeZip(content, caps).read(member)
    if data.startswith(ZIP_MAGIC) and not container:
        raise ArchiveRefused(f"{member!r} is itself an archive; nested archives are not read")
    return data
