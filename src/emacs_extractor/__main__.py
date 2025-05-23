import argparse

from emacs_extractor import extract, finalize
from emacs_extractor.config import (
    load_config_file, load_finalizer_file,
    log_unextracted_files,
    set_emacs_dir, set_unknown_cmd_flags,
)
from emacs_extractor.utils import dataclass_deep_to_json


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
    dumps = dataclass_deep_to_json(extraction)
    if args.output:
        with open(args.output, 'w') as f:
            f.write(dumps)
    else:
        print(dumps)
    finalize(extraction)
    log_unextracted_files()


if __name__ == '__main__':
    entry_point()
