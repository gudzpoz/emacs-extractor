from dataclasses import dataclass, is_dataclass, asdict
from typing import Any, Callable, Union, cast

from emacs_extractor.constants import CConstant
from emacs_extractor.extractor import FileContents
from emacs_extractor.subroutines import Subroutine
from emacs_extractor.variables import CVariable, LispSymbol, LispVariable


class IdHash:
    def __eq__(self, value: object) -> bool:
        return id(self) == id(value)
    def __hash__(self) -> int:
        return id(self)

@dataclass(eq=False)
class PELispVariable(IdHash):
    lisp_name: str

@dataclass(eq=False)
class PECVariable(IdHash):
    c_name: str
    local: bool

@dataclass(eq=False)
class PELispSymbol(IdHash):
    lisp_name: str

@dataclass(eq=False)
class PELispForm(IdHash):
    function: str
    arguments: list['PEValue']

@dataclass(eq=False)
class PECFunctionCall(IdHash):
    c_name: str
    arguments: list['PECValue']

@dataclass(eq=False)
class PELispVariableAssignment(IdHash):
    lisp_name: str
    value: 'PEValue'

@dataclass(eq=False)
class PECVariableAssignment(IdHash):
    c_name: str
    value: 'PECValue'
    local: bool

PEValue = Union[
    PELispVariable,
    PECVariable,
    PELispSymbol,
    PELispForm,
    PECFunctionCall,
    PELispVariableAssignment,
    PECVariableAssignment,
]
PECValue = Union[PEValue | int | str]


PE_EXTERNAL_VARS = {
    'MOST_POSITIVE_FIXNUM': 0,
    'MOST_NEGATIVE_FIXNUM': 0,
    'emacs_wd': False,
}
PE_C_FUNCTIONS = {
    'make_string', # (const char *, ptrdiff_t)
    'make_vector', # (ptrdiff_t, Lisp_Object)
    'make_float', # (double)
    'make_fixnum', # (long)

    'make_symbol_constant', # (Lisp_Object)
    'make_symbol_special', # (Lisp_Object)

    'set_char_table_purpose', # (Lisp_Object, Lisp_Object)
    'set_char_table_defalt', # (Lisp_Object, Lisp_Object)
    'char_table_set_range', # (Lisp_Object, int, int, Lisp_Object)

    'decode_env_path', # (const char * env_name, const char *default, bool empty)

    'malloc', # (size_t)
    'memset', # (void *, int, size_t)
}
# We are treating pure objects as normal objects.
PE_UTIL_FUNCTIONS = {
    # Strings
    'make_pure_string': lambda s, nchars, _nbytes, _multibyte: PECFunctionCall(
        'make_string',
        [s, nchars],
    ),
    'build_pure_c_string': lambda s: PECFunctionCall(
        'make_string',
        [cast(str, s), len(cast(str, s).encode())],
    ),
    'build_unibyte_string': lambda s: PECFunctionCall('make_string', [s, len(s)]),
    'build_string': lambda s: PECFunctionCall('make_string', [s, len(s.encode())]),

    # Symbols
    'intern_c_string': lambda s: PELispSymbol(s),
    'intern': lambda s: PELispSymbol(s),

    # Lists
    'pure_cons': lambda car, cdr: PELispForm('cons', [car, cdr]),
    'pure_list': lambda *args: PELispForm(
        'list', list(cast(list[PEValue], args)),
    ),
    'list1': lambda car: PELispForm('list', [car]),
    'list2': lambda car, cdr: PELispForm('list', [car, cdr]),
    'list3': lambda car, cdr, cddr: PELispForm('list', [car, cdr, cddr]),
    'list4': lambda car, cdr, cddr, cdddr: PELispForm('list', [car, cdr, cddr, cdddr]),
    'nconc2': lambda car, cdr: PELispForm('nconc', [car, cdr]),

    # Vectors
    'make_pure_vector': lambda n: PECFunctionCall(
        'make_vector',
        [n, PELispSymbol('nil')],
    ),
    'make_nil_vector': lambda n: PECFunctionCall(
        'make_vector',
        [n, PELispSymbol('nil')],
    ),
    'ASET': lambda vec, index, value: PELispForm('aset', [vec, index, value]),
    'AREF': lambda vec, index: PELispForm('aref', [vec, index]),

    # Char-tables
    'CHAR_TABLE_SET': lambda table, index, value: PELispForm
}


class PartialEvaluator(dict):
    files: list[FileContents]
    """All files being evaluated."""

    constants: dict[str, CConstant]
    """All constants in all files."""

    lisp_variables: dict[str, LispVariable]
    """All Lisp variables in all files."""

    lisp_symbols: dict[str, LispSymbol]
    """All Lisp symbols in all files."""

    lisp_functions: dict[str, Subroutine]

    c_variables: dict[str, CVariable]
    """All C global variables in all files."""

    _current: FileContents
    """The current file being evaluated."""

    _initialization: dict[str, PEValue]
    """Initialization forms for the current file."""

    _static_variables: dict[str, CVariable]
    """Static C variables in the current file."""

    _local_variables: set[str]
    """Local C variables in the current function."""

    _globals: dict[str, Any]

    _extra_globals: dict[str, Any]

    _evaluated: list[PEValue | None]
    """All evaluated forms."""

    _potential_side_effects: dict[Any, int]
    """All forms that may have side effects.

    Some of them are to be removed after getting incorporated into other forms.
    The values are indices into _evaluated."""

    def __init__(
            self,
            all_symbols: list[LispSymbol],
            files: list[FileContents],
    ):
        self._current = cast(FileContents, None)
        self.files = files
        self.constants = {}
        self.lisp_variables = {}
        self.lisp_functions = {}
        self.c_variables = {}
        for file in files:
            self.constants.update((c.name, c) for c in file.constants)
            self.lisp_variables.update((v.c_name, v) for v in file.lisp_variables)
            self.lisp_functions.update((f.c_name, f) for f in file.functions)
            self.c_variables.update((v.c_name, v) for v in file.c_variables if not v.static)
        self.lisp_symbols = { s.c_name: s for s in all_symbols }
        self._globals = {
            'NULL': 0,
            'ARRAYELTS': len,
            'lispsym': [PELispSymbol(s.lisp_name) for s in all_symbols],
            'sizeof': lambda x: PECFunctionCall('sizeof', [x]),
            'c_pointer': lambda _, x: x,
            'c_array': lambda length, initializer: [
                initializer[i] if initializer and len(initializer) > i else None
                for i in range(length)
            ],

            'CALLN': lambda f, *args: f(*args),
            'CALLMANY': lambda f, args: f(*args),
        }
        self._extra_globals = {}
        self._initialization = {}
        self._static_variables = {}
        self._local_variables = set()
        self._evaluated = []
        self._potential_side_effects = {}

    def reset(self, current: FileContents, extra: dict[str, Any]):
        self._initialization = {}
        self.clear()
        self._current = current
        self._extra_globals = extra
        self._static_variables = { v.c_name: v for v in current.c_variables if v.static }
        self._local_variables = set()
        self._evaluated = []
        self._potential_side_effects = {}

    def _walk_remove_side_effect(self, v: Any):
        if not is_dataclass(v) or isinstance(v, type):
            return
        for f in asdict(v).values():
            if isinstance(f, list):
                children = f
            elif not is_dataclass(f):
                continue
            children = f if isinstance(f, list) else [f]
            for child in children:
                if not is_dataclass(child):
                    continue
                i = self._potential_side_effects.pop(child, None)
                if i is not None:
                    self._evaluated[i] = None
        index = len(self._evaluated)
        self._evaluated.append(cast(Any, v))
        self._potential_side_effects[v] = index

    def _watch_side_effects(self, v: Callable):
        if callable(v):
            def wrapper(*args):
                nonlocal v
                result = v(*args)
                self._walk_remove_side_effect(result)
                return result
            return wrapper
        return v

    def __missing__(self, key: str):
        if key in self.constants:
            return self.constants[key].value
        if key in self._current.constants:
            return self._current.constants[key].value
        if key in self.lisp_symbols:
            return PELispSymbol(self.lisp_symbols[key].lisp_name)
        if key in self.c_variables:
            return PECVariable(key, False)
        if key in self.lisp_functions:
            return self._watch_side_effects(
                lambda *args: PELispForm(self.lisp_functions[key].lisp_name, list(args)),
            )
        if key == 'void':
            # The tranpiler converts `(void) 0;` to `void(0)` as a no-op.
            return lambda _: None
        if key in PE_UTIL_FUNCTIONS:
            rewrite = PE_UTIL_FUNCTIONS[key]
            return self._watch_side_effects(rewrite)
        if key in PE_C_FUNCTIONS:
            return self._watch_side_effects(lambda *args: PECFunctionCall(key, list(args)))
        if key in PE_EXTERNAL_VARS:
            v = PE_EXTERNAL_VARS[key]
            if v is None or v == False:
                return v
            return PECVariable(key, False)
        if key in self.lisp_variables:
            v = self.lisp_variables[key]
            # Init values
            if v.lisp_type == 'BOOL':
                return False
            if v.lisp_type == 'INT':
                return 0
        if key in self._extra_globals:
            v = self._extra_globals[key]
            return self._watch_side_effects(v)
        if key in self._globals:
            return self._globals[key]
        raise KeyError(key)

    def __setitem__(self, key: str, value: Any) -> None:
        local = False
        if key in self.lisp_variables:
            self._initialization[key] = value
            self._evaluated.append(
                PELispVariableAssignment(self.lisp_variables[key].lisp_name, value),
            )
            recorded = PELispVariable(self.lisp_variables[key].lisp_name)
        elif key in self.c_variables:
            self._initialization[key] = value
            self._evaluated.append(PECVariableAssignment(key, value, False))
            recorded = PECVariable(self.c_variables[key].c_name, False)
        else:
            local = True
            self._local_variables.add(key)
            self._initialization[key] = value
            if is_dataclass(value):
                recorded = PECVariable(key, True)
            else:
                recorded = value
            self._evaluated.append(PECVariableAssignment(key, cast(Any, value), local))
        return super(PartialEvaluator, self).__setitem__(key, recorded)

    def evaluate(self, code: str, current: FileContents, extra_globals: dict[str, Any]):
        self.reset(current, extra_globals)
        exec(code, self)
        return self._evaluated
