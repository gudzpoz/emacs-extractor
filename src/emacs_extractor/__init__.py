from emacs_extractor.config import (
    EmacsExtraction, InitFunction,
    get_config, get_emacs_dir, get_finalizer,
)
from emacs_extractor.extractor import EmacsExtractor
from emacs_extractor.partial_eval import PartialEvaluator
from emacs_extractor.transpiler import CTranspiler


def extract() -> EmacsExtraction:
    config = get_config()
    src_dir = get_emacs_dir()

    extractor = EmacsExtractor(
        src_dir,
        config.files,
        config.function_specific_configs,
        config.ignored_constants,
        config.extra_macros,
        config.extra_extraction_constants,
    )
    files, all_symbols, init_functions = extractor.extract_static()

    transpiler = CTranspiler(
        init_functions,
        { constant.name for file in files for constant in file.constants },
        config.function_specific_configs,
        config.ignored_functions,
    )
    pe = PartialEvaluator(
        all_symbols,
        files,
        config.pe_c_functions,
        config.pe_util_functions,
        config.pe_eliminate_local_vars,
    )
    initializations: list[InitFunction] = []
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
            if local_config and local_config.statement_remapper:
                statements = local_config.statement_remapper(statements, pe)
            initializations.append(InitFunction(call.call, file.file.name, statements))
        except Exception as e:
            lines = transpiled.splitlines()
            for i, line in enumerate(lines):
                print(f'{i + 1:4d}: {line}')
            raise e

    return EmacsExtraction(
        all_symbols=all_symbols,
        file_extractions=files,
        initializations=initializations,
    )


def finalize(extraction: EmacsExtraction) -> None:
    finalizer = get_finalizer()
    if finalizer:
        finalizer(extraction)
