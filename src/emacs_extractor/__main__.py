import argparse
import json
import dataclasses
from pathlib import Path
import types

from emacs_extractor import extract, finalize
from emacs_extractor.config import (
    load_config_file, set_emacs_dir, set_unknown_cmd_flags,
    load_finalizer_file,
)


def default(o):
    if isinstance(o, Path):
        return o.name
    if dataclasses.is_dataclass(o) and not isinstance(o, type):
        d = dict(o.__dict__)
        d['$type'] = type(o).__name__
        return d
    raise TypeError(f'Object of type {type(o)} is not JSON serializable')


def entry_point():
    parser = argparse.ArgumentParser(description='Emacs extractor')
    parser.add_argument('src_dir', type=str, help='Emacs source directory')
    parser.add_argument('-c', '--config', type=str, required=True, help='Config file')
    parser.add_argument('-f', '--finalizer', type=str, help='Finalizer script')
    parser.add_argument('-o', '--output', type=str, help='Output JSON file')
    args, unknown = parser.parse_known_args()
    set_unknown_cmd_flags(unknown)
    set_emacs_dir(args.src_dir)
    load_config_file(args.config)
    if args.finalizer:
        load_finalizer_file(args.finalizer)
    extraction = extract()
    dumps = json.dumps(extraction, default=default, indent=2)
    if args.output:
        with open(args.output, 'w') as f:
            f.write(dumps)
    else:
        print(dumps)
    finalize(extraction)


if __name__ == '__main__':
    entry_point()
