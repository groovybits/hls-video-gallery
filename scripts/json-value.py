#!/usr/bin/env python3
"""Print one scalar value from a JSON object using a dotted key."""

import argparse
import json
from pathlib import Path
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_file")
    parser.add_argument("key")
    arguments = parser.parse_args()
    try:
        value = json.loads(Path(arguments.json_file).read_text(encoding="utf-8"))
        for portion in arguments.key.split("."):
            value = value[portion]
    except (OSError, ValueError, TypeError, KeyError) as error:
        print("Cannot read {} from {}: {}".format(arguments.key, arguments.json_file, error), file=sys.stderr)
        return 2
    if isinstance(value, bool):
        print("true" if value else "false")
    elif isinstance(value, (str, int, float)):
        print(value)
    else:
        print("Requested value is not scalar: {}".format(arguments.key), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
