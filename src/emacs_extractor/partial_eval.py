from dataclasses import dataclass, is_dataclass, asdict
from typing import Any, Callable, Union, cast

from emacs_extractor.constants import CConstant
from emacs_extractor.extractor import FileContents
from emacs_extractor.subroutines import Subroutine
from emacs_extractor.variables import CVariable, LispSymbol, LispVariable


@dataclass
class PEIntConstant:
    '''Referring to a constant.'''
    c_name: str
    value: int

@dataclass
class PELispVariable:
    '''Referring to the value of a lisp variable defined with `DEFVAR_*`.'''
    lisp_name: str

@dataclass
class PECVariable:
    '''Referring to the value of a C variable.'''
    c_name: str
    local: bool
    '''True if the variable is a function-local variable.'''

@dataclass
class PELispSymbol:
    '''Referring to a lisp symbol (e.g., Qnil).'''
    lisp_name: str

@dataclass
class PELispForm:
    '''Calls a built-in subroutine. For some vararg subroutines,
    the arguments may be like [arg_count, arg_array], or maybe not.'''
    function: str
    arguments: list['PEValue']

@dataclass
class PECFunctionCall:
    '''Calls a C function.'''
    c_name: str
    arguments: list['PECValue']

@dataclass
class PELispVariableAssignment:
    '''Assigning a value to a lisp variable defined with `DEFVAR_*`.'''
    lisp_name: str
    value: 'PEValue'

@dataclass
class PECVariableAssignment:
    '''Assigning a value to a C variable.'''
    c_name: str
    value: 'PECValue'
    local: bool
    '''True if the variable is a function-local variable.'''

PEValue = Union[
    PEIntConstant,
    PELispVariable,
    PECVariable,
    PELispSymbol,
    PELispForm,
    PECFunctionCall,
    PELispVariableAssignment,
    PECVariableAssignment,
]

PEInt = int | PEIntConstant

@dataclass
class PELiteral:
    '''A utility class representing values preserved as is.

    Not used by the evaluator. Only provided for the convenience of extraction configs
    and finalizers.'''
    value: str

PECValue = Union[PEValue | PELiteral | int | str | float | bool]


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
            pe_c_functions: set[str],
            pe_util_functions: dict[str, Callable],
    ):
        self._current = cast(FileContents, None)
        self.files = files
        self.pe_c_functions = pe_c_functions
        self.pe_util_functions = pe_util_functions
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
        if is_dataclass(v):
            i = self._potential_side_effects.pop(id(v), None)
            if i is not None:
                self._evaluated[i] = None
        return v

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

    def _get_constant(self, key: str) -> int | str | None:
        if key in self.constants:
            return self.constants[key].value
        if key in self._current.constants:
            return self._current.constants[key].value
        return None

    def _try_get_constant(self, key: str) -> PECValue:
        constant = self._get_constant(key)
        if isinstance(constant, int):
            return PEIntConstant(key, constant)
        if constant is not None:
            return constant
        return self[key]

    def __missing__(self, key: str):
        constant = self._get_constant(key)
        if constant is not None:
            return constant
        if key in self.lisp_symbols:
            return PELispSymbol(self.lisp_symbols[key].lisp_name)
        if key in self.c_variables:
            return PECVariable(key, False)
        if key in self.lisp_functions:
            return self._watch_side_effects(
                lambda *args: PELispForm(self.lisp_functions[key].lisp_name, (
                    ([] if args[1] == 0 else list(args[1]))
                    if len(args) == 2 and isinstance(args[0], PEInt)
                    else list(args)
                )),
            )
        if key == 'void':
            # The tranpiler converts `(void) 0;` to `void(0)` as a no-op.
            return lambda _: None
        if key == 'PE_CONSTANT':
            return self._try_get_constant
        if key == 'PRUNE_SIDE_EFFECT':
            return lambda v: self._remove_side_effects(v)
        if key in self.pe_util_functions:
            rewrite = self.pe_util_functions[key]
            return self._watch_side_effects(rewrite)
        if key in self.pe_c_functions:
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
        if isinstance(v, PECFunctionCall):
            match v.c_name:
                case 'make_fixnum' if isinstance(v.arguments[0], PEInt):
                    v = v.arguments[0]
                case 'make_float' if isinstance(v.arguments[0], float):
                    v = v.arguments[0]
                case 'make_string' if isinstance(v.arguments[0], str):
                    v = v.arguments[0]
        if isinstance(v, (PEInt, float, bool, str)):
            return v, True
        return v, False

    def __setitem__(self, key: str, value: Any) -> None:
        self._walk_remove_side_effect(value)
        if key in self.lisp_variables:
            var = self.lisp_variables[key]
            init = False
            simplified, is_simple = self._to_simple(value)
            if var.init_value is None or var.init_value == simplified:
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
