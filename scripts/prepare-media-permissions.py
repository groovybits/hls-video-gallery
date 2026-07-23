#!/usr/bin/env python3
"""Normalize newly uploaded source-video ownership and read permissions safely."""

import argparse
import os
from pathlib import Path
import pwd
import stat


VIDEO_EXTENSIONS = {
    ".3gp", ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov", ".mp4",
    ".mpeg", ".mpg", ".mts", ".mxf", ".ogv", ".ts", ".webm", ".wmv",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--media-dir", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--quiet", action="store_true")
    arguments = parser.parse_args()

    media_dir = Path(arguments.media_dir).expanduser().resolve()
    if not media_dir.is_dir():
        raise SystemExit("Media directory does not exist: {}".format(media_dir))
    account = pwd.getpwnam(arguments.owner)

    changed = 0
    errors = 0
    for path in media_dir.rglob("*"):
        try:
            file_stat = path.lstat()
            if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
                continue
            if path.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            needs_owner = file_stat.st_uid != account.pw_uid or file_stat.st_gid != account.pw_gid
            needs_mode = stat.S_IMODE(file_stat.st_mode) != 0o644
            if needs_owner:
                os.chown(str(path), account.pw_uid, account.pw_gid, follow_symlinks=False)
            if needs_mode:
                os.chmod(str(path), 0o644, follow_symlinks=False)
            if needs_owner or needs_mode:
                changed += 1
        except OSError as error:
            errors += 1
            print("Could not normalize {}: {}".format(path, error))

    if not arguments.quiet or changed or errors:
        print("Media permissions ready: {} changed, {} errors".format(changed, errors))
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
