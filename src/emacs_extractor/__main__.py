import argparse
import json
import dataclasses
from pathlib import Path
import types

from emacs_extractor import extract
from emacs_extractor.config import load_config_file, set_emacs_dir


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
    parser.add_argument('-o', '--output', type=str, help='Output JSON file')
    args = parser.parse_args()
    set_emacs_dir(args.src_dir)
    load_config_file(args.config)
    extraction = extract()
    dumps = json.dumps(extraction, default=default, indent=2)
    if args.output:
        with open(args.output, 'w') as f:
            f.write(dumps)
    else:
        print(dumps)


if __name__ == '__main__':
    entry_point()
