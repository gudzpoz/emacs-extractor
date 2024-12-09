import argparse

from emacs_extractor import EmacsExtractor, CTranspiler
from emacs_extractor.config import get_config, load_config_file, set_emacs_dir
from emacs_extractor.partial_eval import PartialEvaluator


def entry_point():
    parser = argparse.ArgumentParser(description='Emacs extractor')
    parser.add_argument('src_dir', type=str, help='Emacs source directory')
    parser.add_argument('-c', '--config', type=str, required=True, help='Config file')
    args = parser.parse_args()
    set_emacs_dir(args.src_dir)
    load_config_file(args.config)
    config = get_config()

    extractor = EmacsExtractor(
        args.src_dir,
        config.files,
        config.function_specific_configs,
        config.extra_macros,
        config.extra_extraction_constants,
    )
    files, all_symbols, init_functions = extractor.extract_static()

    transpiler = CTranspiler(
        init_functions,
        config.function_specific_configs,
        config.ignored_functions,
    )
    pe = PartialEvaluator(all_symbols, files)
    for call in extractor.init_calls:
        if call.call not in init_functions:
            continue
        file = init_functions[call.call][1]
        transpiled = transpiler.transpile_to_python(call.call)
        try:
            local_config = config.function_specific_configs.get(call.call)
            extra_globals = local_config.extra_globals if local_config else {}
            statements = pe.evaluate(
                transpiled,
                file,
                extra_globals or {},
            )
        except Exception as e:
            lines = transpiled.splitlines()
            for i, line in enumerate(lines):
                print(f'{i + 1:4d}: {line}')
            raise e


if __name__ == '__main__':
    entry_point()
