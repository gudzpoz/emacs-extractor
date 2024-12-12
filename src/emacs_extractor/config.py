from pathlib import Path
import sys
import typing

from dataclasses import dataclass
from importlib.util import module_from_spec, spec_from_file_location

from tree_sitter import Node

from emacs_extractor.extractor import FileContents
from emacs_extractor.partial_eval import PartialEvaluator, PEValue
from emacs_extractor.utils import require_not_none
from emacs_extractor.variables import LispSymbol


@dataclass
class InitFunction:
    name: str
    file: str
    statements: list[PEValue]

@dataclass
class EmacsExtraction:
    '''Extraction output.'''

    all_symbols: list[LispSymbol]
    '''All symbols defined in `globals.h`.'''

    file_extractions: list[FileContents]
    '''All the extracted files.'''

    initializations: list[InitFunction]
    '''An AST-ish thing of the initialization functions called in `main` in `emacs.c`,
    in the order they are called.'''


@dataclass
class SpecificConfig:
    '''Configuration for a specific C function.'''

    transpile_replaces: list[str | tuple[str, str]] | None = None
    '''Regexps to replace/comment out a part of the transpiled code.

    The replace happens on a per-line basis for the transpiled output.
    When an item is a tuple, the first element is the regex to match,
    and the second element is the replacement string.
    If the item is a string, it is treated as a regex to match and the transpiler
    will prepend `# ` to the line to comment it out.'''

    extra_globals: dict[str, typing.Any] | None = None
    '''Extra globals to be added to the evaluation context.'''

    extra_extraction: typing.Callable[
        [dict[str, 'SpecificConfig'], Node, dict[str, LispSymbol]],
        None
    ] | None = None
    '''Extra extraction logic to be run after the default extraction logic.'''

    statement_remapper: typing.Callable[
        [list[PEValue], PartialEvaluator],
        list[PEValue]
    ] | None = None
    '''Rewrite the statements of the function.'''


@dataclass
class EmacsExtractorConfig:
    '''Configuration for the Emacs Extractor.'''

    files: list[str]
    '''File names of the files to extract from.'''

    extra_macros: str
    '''Extra macros (or any string) to be prepended to each file when extracting & executing code.'''

    extra_extraction_constants: dict[str, int | str]
    '''Extra constants to be added to the extraction context.

    To handle `enum { A = B + 1 };` in C code, we actually `eval` values like `B + 1`.
    So this field serves to provide concrete values for things like `NULL` in the eval context.'''

    ignored_constants: set[str]
    '''Constants to be ignored when extracting constants.

    Currently, these mostly include constants requiring `sizeof` or `offsetof`.'''

    ignored_functions: set[str]
    '''Init functions to be ignored when partial-evaluating the code.'''

    pe_c_functions: set[str]
    '''Functions to be treated as C functions when partial-evaluating the code.'''

    pe_util_functions: dict[str, typing.Callable]
    '''Extra global functions to be placed in the evaluation context.
    Mainly used to transform some utility functions into common functions.'''

    function_specific_configs: dict[str, SpecificConfig]
    '''Configuration for specific init functions.'''

    pe_eliminate_local_vars: bool = False
    '''Whether to aggresively inline variable values.

    For example, with `pe_eliminate_local_vars = False`, the code
    `Vvar_a = 1; Vvar_b = Ffun(Vvar_a); Vvar_a = Fdull(Vvar_b);` will produce:
    - `Vvar_a[init_value=1]`
    - `Vvar_b[init_value=None(not set)]`
    - Init statements:
      - `Vvar_b = Ffun(1)`
      - `Vvar_a = Fdull(Vvar_b)`
    With `pe_eliminate_local_vars = True`, the code will produce:
    - `Vvar_a[init_value=Fdull(Ffun(1))]`
    - `Vvar_b[init_value=Ffun(1)]`
      - When `Vvar_b` is a local variable instead of a lisp var,
        it will be eliminated.

    I don't know if this handles complex initialization correctly.
    Use with caution.'''


_config: EmacsExtractorConfig | None = None


def get_config() -> EmacsExtractorConfig:
    return require_not_none(_config)

def set_config(config: EmacsExtractorConfig):
    '''Set the config for the Emacs Extractor.

    Should be called by the config Python file.'''
    global _config
    _config = config

def load_config_file(python_file: str):
    spec = spec_from_file_location('extraction_config', python_file)
    assert spec is not None
    module = module_from_spec(spec)
    sys.modules['extraction_config'] = module
    require_not_none(spec.loader).exec_module(module)


_emacs_dir: Path | None = None


def get_emacs_dir() -> Path:
    return require_not_none(_emacs_dir)

def set_emacs_dir(emacs_dir: str):
    global _emacs_dir
    _emacs_dir = Path(emacs_dir)
    assert _emacs_dir.exists() and _emacs_dir.is_dir()


_unknown_cmd_flags: list[str] = []


def get_unknown_cmd_flags() -> list[str]:
    return _unknown_cmd_flags

def set_unknown_cmd_flags(flags: list[str]):
    global _unknown_cmd_flags
    _unknown_cmd_flags = flags


_finalizer: typing.Callable[[EmacsExtraction], None] | None = None


def get_finalizer():
    return _finalizer

def set_finalizer(finalizer: typing.Callable[[EmacsExtraction], None]):
    '''Set the finalizer for the Emacs Extractor.

    Should be called by the finalizer Python file.'''
    global _finalizer
    _finalizer = finalizer

def load_finalizer_file(python_file: str):
    spec = spec_from_file_location('finalizer', python_file)
    assert spec is not None
    module = module_from_spec(spec)
    sys.modules['finalizer'] = module
    require_not_none(spec.loader).exec_module(module)
