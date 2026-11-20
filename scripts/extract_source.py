"""Records which source files an analysis extract was built from, as rows
for its extract_info table.

Added 2026-11-19: the sda project asked which copy of sda.db its extract
came from, and extract_info didn't say (it was a scratch pull of the live
db, since deleted). Each builder now records, per input db:

- <key>_path, <key>_bytes, <key>_mtime (UTC), <key>_sha1: the file as read,
  hashed before the build starts. sha1 identifies a copy exactly, e.g. to
  check whether a dated snapshot is the same file.
- <key>_wal_bytes, only if a non-empty -wal file sits beside it: some data
  is then in the WAL rather than the main file, so the sha1 alone doesn't
  pin the content (alphanume.db runs in WAL mode).
- <key>_origin: where the local file came from, which a file can't tell
  you, passed as --<name>-origin. For a pulled copy, give the remote and
  its ModTime at pull time, e.g.
    --sda-db-origin "onedrive:Data/StockDataAnalytics/mars/sda.db ModTime 2026-10-30T09:09:25Z"
  (rclone lsjson <remote> | jq -r '.[0].ModTime'). "not recorded" if
  omitted.
"""
import argparse
import hashlib
from datetime import datetime, timezone
from pathlib import Path


def add_origin_arg(parser: argparse.ArgumentParser, name: str) -> None:
    """Adds --<name>-origin, e.g. name "sda-db" -> --sda-db-origin."""
    parser.add_argument(
        f"--{name}-origin", default=None,
        help=f"Where --{name} came from, recorded in extract_info: for a pulled "
             "copy, the remote and its ModTime at pull time.")


def source_info(key: str, path: Path, origin: str | None = None) -> dict[str, str]:
    """extract_info rows describing one source file (see module docstring)."""
    path = Path(path).expanduser().resolve()
    st = path.stat()
    sha1 = hashlib.sha1()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            sha1.update(chunk)
    info = {
        f"{key}_path": str(path),
        f"{key}_bytes": str(st.st_size),
        f"{key}_mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc)
                                .isoformat(timespec="seconds"),
        f"{key}_sha1": sha1.hexdigest(),
    }
    wal = path.with_name(path.name + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        info[f"{key}_wal_bytes"] = str(wal.stat().st_size)
    info[f"{key}_origin"] = origin or "not recorded"
    return info
