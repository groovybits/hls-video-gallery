#!/usr/bin/env python3
"""Install a rendered site tree without overwriting runtime media or catalogs."""

import argparse
import grp
import os
from pathlib import Path
import pwd
import shutil


PRESERVE_IF_PRESENT = {
    "data/catalog.json",
    "data/content-index.json",
    "data/content-overrides.json",
    "media/PUT-VIDEOS-HERE.txt",
}
PRIVATE_FILES = {".share-common.php", "data/content-overrides.json"}
EXECUTABLE_DIR = "_tools"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--owner", required=True)
    arguments = parser.parse_args()

    source = Path(arguments.source).resolve()
    target = Path(arguments.target).resolve()
    account = pwd.getpwnam(arguments.owner)
    group = grp.getgrgid(account.pw_gid)
    if not source.is_dir():
        raise SystemExit("Rendered site tree is missing: {}".format(source))

    for directory in sorted([source] + [path for path in source.rglob("*") if path.is_dir()]):
        relative = directory.relative_to(source)
        destination = target / relative
        destination.mkdir(mode=0o755, parents=True, exist_ok=True)
        os.chown(str(destination), account.pw_uid, group.gr_gid)
        if relative.parts and relative.parts[0] in {"cache", "data", "media"}:
            os.chmod(str(destination), 0o755)

    copied = 0
    for path in sorted(item for item in source.rglob("*") if item.is_file()):
        relative = path.relative_to(source)
        relative_text = relative.as_posix()
        destination = target / relative
        if relative_text in PRESERVE_IF_PRESENT and destination.exists():
            continue
        mode = 0o755 if relative.parts and relative.parts[0] == EXECUTABLE_DIR else 0o644
        if relative_text in PRIVATE_FILES:
            mode = 0o600
        temporary = destination.with_name("." + destination.name + ".installing")
        shutil.copyfile(str(path), str(temporary))
        os.chmod(str(temporary), mode)
        os.chown(str(temporary), account.pw_uid, group.gr_gid)
        os.replace(str(temporary), str(destination))
        copied += 1
    print("Installed {} application files into {}".format(copied, target))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
