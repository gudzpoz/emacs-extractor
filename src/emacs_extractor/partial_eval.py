from dataclasses import dataclass, is_dataclass, asdict
from typing import Any, Callable, Union, cast

from emacs_extractor.constants import CConstant
from emacs_extractor.extractor import FileContents
from emacs_extractor.subroutines import Subroutine
from emacs_extractor.variables import CVariable, LispSymbol, LispVariable


@dataclass(eq=False)
class PELispVariable:
    lisp_name: str

@dataclass(eq=False)
class PECVariable:
    c_name: str
    local: bool

@dataclass(eq=False)
class PELispSymbol:
    lisp_name: str

@dataclass(eq=False)
class PELispForm:
    function: str
    arguments: list['PEValue']

@dataclass(eq=False)
class PECFunctionCall:
    c_name: str
    arguments: list['PECValue']

@dataclass(eq=False)
class PELispVariableAssignment:
    lisp_name: str
    value: 'PEValue'

@dataclass(eq=False)
class PECVariableAssignment:
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
PECValue = Union[PEValue | int | str | float | bool]


PE_C_FUNCTIONS = {
    'make_string', # (const char *, ptrdiff_t)
    'make_vector', # (ptrdiff_t, Lisp_Object)
    'make_float', # (double)
    'make_fixnum', # (long)
    'make_int', # (long)

    'make_symbol_constant', # (Lisp_Object)
    'make_symbol_special', # (Lisp_Object)

    'make_hash_table', # (hash_table_test, int size, weakness, bool purecopy)

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
    'listn': lambda _n, *args: PELispForm('list', list(args)),
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
    'CHAR_TABLE_SET': lambda table, index, value: PECFunctionCall(
        'char_table_set', [table, index, value],
    ),
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
    """All Lisp functions in all files."""

    c_variables: dict[str, CVariable]
    """All C global variables in all files."""

    _current: FileContents
    """The current file being evaluated."""

    _static_variables: dict[str, CVariable]
    """Static C variables in the current file."""

    _local_variables: set[str]
    """Local C variables in the current function."""

    _globals: dict[str, Any]

    _extra_globals: dict[str, Any]

    _evaluated: list[PEValue | None]
    """All evaluated forms."""

    _potential_side_effects: dict[int, int]
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
        self._static_variables = {}
        self._local_variables = set()
        self._evaluated = []
        self._potential_side_effects = {}

    def reset(self, current: FileContents, extra: dict[str, Any]):
        self.clear()
        self._current = current
        self._extra_globals = extra
        self._static_variables = { v.c_name: v for v in current.c_variables if v.static }
        self._local_variables = set()
        self._evaluated = []
        self._potential_side_effects = {}

    def _remove_side_effects(self, v: Any):
        if not is_dataclass(v):
            return
        i = self._potential_side_effects.pop(id(v), None)
        if i is not None:
            self._evaluated[i] = None

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
                self._remove_side_effects(child)
        self._remove_side_effects(v)

    def _watch_side_effects(self, v: Callable):
        if callable(v):
            def wrapper(*args):
                nonlocal v
                result = v(*args)
                for arg in args:
                    self._walk_remove_side_effect(arg)
                if (is_dataclass(result) and not isinstance(result, type)
                    and id(result) not in self._potential_side_effects
                ):
                    index = len(self._evaluated)
                    self._evaluated.append(cast(Any, result))
                    self._potential_side_effects[id(result)] = index
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
        if key in self.lisp_variables:
            v = self.lisp_variables[key]
            # Init values
            if v.lisp_type == 'BOOL':
                return False
            if v.lisp_type == 'INT':
                return 0
            return PELispVariable(v.lisp_name)
        if key in self._extra_globals:
            v = self._extra_globals[key]
            return self._watch_side_effects(v)
        if key in self._globals:
            return self._watch_side_effects(self._globals[key])
        raise KeyError(key)

    def _to_simple(self, v: Any) -> tuple[Any, bool]:
        if isinstance(v, PELispSymbol):
            if v.lisp_name == 't':
                v = True
            elif v.lisp_name == 'nil':
                v = False
        if isinstance(v, (int, float, bool)):
            return v, True
        return v, False

    def __setitem__(self, key: str, value: Any) -> None:
        self._walk_remove_side_effect(value)
        if key in self.lisp_variables:
            var = self.lisp_variables[key]
            init = False
            if var.init_value is None:
                simplified, is_simple = self._to_simple(value)
                if is_simple:
                    value = simplified
                    var.init_value = simplified
                    init = True
            if not init:
                self._evaluated.append(
                    PELispVariableAssignment(self.lisp_variables[key].lisp_name, value),
                )
            recorded = PELispVariable(self.lisp_variables[key].lisp_name)
        elif key in self.c_variables:
            self._evaluated.append(PECVariableAssignment(key, value, False))
            recorded = PECVariable(self.c_variables[key].c_name, False)
        else:
            self._local_variables.add(key)
            if is_dataclass(value) and not isinstance(value, PELispSymbol):
                recorded = PECVariable(key, True)
                self._evaluated.append(PECVariableAssignment(key, cast(Any, value), True))
            else:
                recorded = value
        return super(PartialEvaluator, self).__setitem__(key, recorded)

    @classmethod
    def _pe_constant(cls, statement: PECValue):
        if isinstance(statement, PECVariableAssignment):
            if statement.local and not is_dataclass(statement.value):
                return True
        return False

    def evaluate(self, code: str, current: FileContents, extra_globals: dict[str, Any]):
        self.reset(current, extra_globals)
        exec(code, self)
        return [
            statement
            for statement in self._evaluated
            if statement is not None and not self._pe_constant(statement)
        ]
