from dataclasses import dataclass
from emacs_extractor.config import EmacsExtraction, InitFunction, get_config, get_emacs_dir
from emacs_extractor.extractor import EmacsExtractor
from emacs_extractor.partial_eval import PECVariableAssignment, PartialEvaluator, PEValue
from emacs_extractor.transpiler import CTranspiler


def meaningful(statement: PEValue) -> bool:
    if isinstance(statement, PECVariableAssignment):
        return not (isinstance(statement.value, int) and statement.local)
    return True


def extract() -> EmacsExtraction:
    config = get_config()
    src_dir = get_emacs_dir()

    extractor = EmacsExtractor(
        src_dir,
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
            statements = [s for s in statements if s is not None and meaningful(s)]
            if local_config and local_config.statement_remapper:
                statements = local_config.statement_remapper(statements, pe)
            initializations.append(InitFunction(call.call, file.file.name, statements))
        except Exception as e:
            lines = transpiled.splitlines()
            for i, line in enumerate(lines):
                print(f'{i + 1:4d}: {line}')
            raise e

    extraction = EmacsExtraction(
        all_symbols=all_symbols,
        file_extractions=files,
        initializations=initializations,
    )
    if config.finalizer:
        config.finalizer(extraction)
    return extraction