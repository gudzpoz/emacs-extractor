import argparse
import builtins
import json
import re
from emacs_extractor.config import (
    EmacsExtraction, InitFunction,
    get_unknown_cmd_flags, set_finalizer,
)
from emacs_extractor.partial_eval import *


def replace_or_insert_region(
        contents: str,
        marker: str,
        update: str,
        indents: int = 4,
        insertion: bool = True
):
    '''Replace or insert a region in a file.

    The region is marked by `marker` like `//#region {marker}`.

    - `contents`: The contents of the file.
    - `marker`: The marker of the region.
    - `update`: The new contents of the region.
    - `indents`: The number of indents to use for the region region marker.
    - `insertion`: Whether to insert the region if it does not exist.'''
    section_start = (
        f'{' ' * indents}//#region {marker}\n'
    )
    section_end = (
        f'{' ' * indents}//#endregion {marker}\n'
    )
    if not insertion or section_start in contents:
        start = contents.index(section_start)
        end = contents.index(section_end)
        assert start < end
        original = contents[start:end]
        original = re.sub(r'^\s*//.*$', '', original, flags=re.MULTILINE)
        original = re.sub(r'\s+', '', original, flags=re.MULTILINE)
        sub = re.sub(r'\s+', '', update, flags=re.MULTILINE)
        if original == sub:
            # Preserve comments
            return contents
        else:
            return (
                f'{contents[:start]}{section_start}'
                f'{update}{contents[end:]}'
            )
    else:
        last = contents.rfind('}')
        return (
            f'{contents[:last]}{section_start}{update}'
            f'{section_end}{contents[last:]}'
        )


def generate_java_symbol_init(symbols: list[tuple[str, str]], init_function: str):
    java_symbols = '\n'.join(
        f'    public static final ELispSymbol {symbol[0]} = '
        f'new ELispSymbol({json.dumps(symbol[1])});'
        for symbol in symbols
    )
    all_symbols = f'''
    private ELispSymbol[] {init_function}() {{
        return new ELispSymbol[] {{
{'\n'.join(f'            {symbol[0]},' for symbol in symbols)}
        }};
    }}
'''
    return java_symbols + all_symbols


def export_symbols(extraction: EmacsExtraction, output_file: str):
    with open(output_file, 'r') as f:
        contents = f.read()

    # DEFSYM
    assert all(symbol.c_name.startswith('Q') for symbol in extraction.all_symbols)
    all_symbols = [(symbol.c_name[1:].upper(), symbol.lisp_name) for symbol in extraction.all_symbols]
    defined = dict(all_symbols)
    defined_lisp_names = set(symbol[1] for symbol in all_symbols)
    # Variables
    variable_symbols: list[tuple[str, str]] = []
    per_buffer_variables: list[tuple[str, str]] = []
    for file in extraction.file_extractions:
        for var in file.lisp_variables:
            c_name = var.lisp_name.replace('-', '_').upper()
            if c_name in defined:
                if defined[c_name] == var.lisp_name:
                    continue
                c_name = f'_{c_name}'
            if var.lisp_name in defined_lisp_names:
                continue
            variable_symbols.append((c_name, var.lisp_name))
            defined[c_name] = var.lisp_name
            defined_lisp_names.add(var.lisp_name)
        for var in file.per_buffer_variables:
            c_name = var.lisp_name.replace('-', '_').upper()
            if c_name in defined:
                assert defined[c_name] == var.lisp_name
                continue
            if var.lisp_name in defined_lisp_names:
                continue
            per_buffer_variables.append((c_name, var.lisp_name))
            defined[c_name] = var.lisp_name
            defined_lisp_names.add(var.lisp_name)
    variable_symbols = sorted(variable_symbols)
    per_buffer_variables = sorted(per_buffer_variables)

    for symbols, init_function, marker in [
        (all_symbols, 'allSymbols', 'globals.h'),
        (variable_symbols, 'variableSymbols', 'variable symbols'),
        (per_buffer_variables, 'bufferLocalVarSymbols', 'buffer.c'),
    ]:
        contents = replace_or_insert_region(
            contents,
            marker,
            generate_java_symbol_init(symbols, init_function),
        )
    with open(output_file, 'w') as f:
        f.write(contents)

    return { lisp_name: c_name for c_name, lisp_name in defined.items() }


JAVA_KEYWORDS = {
    'abstract',
    'do',
    'if',
    'package',
    'synchronized',
    'boolean',
    'double',
    'implements',
    'private',
    'this',
    'break',
    'else',
    'import',
    'protected',
    'throw',
    'byte',
    'extends',
    'instanceof',
    'public',
    'throws',
    'case',
    'false',
    'int',
    'return',
    'transient',
    'catch',
    'final',
    'interface',
    'short',
    'true',
    'char',
    'finally',
    'long',
    'static',
    'try',
    'class',
    'float',
    'native',
    'strictfp',
    'void',
    'const',
    'for',
    'new',
    'super',
    'volatile',
    'continue',
    'goto',
    'null',
    'switch',
    'while',
    'default',
    'assert',
}
def _c_name_to_java(c_name: str):
    name = ''.join(
        seg.lower() if i == 0 else seg.capitalize()
        for i, seg in enumerate(c_name.split('_'))
    )
    if name in JAVA_KEYWORDS:
        name += '_'
    return name

def _c_name_to_java_class(c_name: str):
    return ''.join(
        seg.capitalize()
        for seg in c_name.split('_')
    )

def _c_var_name_to_java(c_name: str):
    return _c_name_to_java(c_name[1:] if c_name.startswith('V') else c_name)


def export_variables(extraction: EmacsExtraction, symbols: dict[str, str], output_file: str):
    files = []
    variables: dict[str, str] = {}
    for file in sorted(extraction.file_extractions, key=lambda f: f.file.name):
        if file.file.name.endswith('.h'):
            assert len(file.lisp_variables) == 0
            assert len(file.per_buffer_variables) == 0
            continue
        stem = file.file.stem
        files.append(stem)
        var_defs = []
        inits = []
        for var in file.lisp_variables:
            c_name = symbols[var.lisp_name]
            java_name = _c_name_to_java(c_name)
            suffix = ''
            match var.lisp_type:
                case 'INT':
                    t = 'ForwardedLong'
                case 'BOOL':
                    t = 'ForwardedBool'
                case 'LISP':
                    t = 'Forwarded'
                case 'KBOARD':
                    t = 'Forwarded'
                    suffix = ' /* TODO */'
            var_defs.append(
                f'    private static final ELispSymbol.Value.{t} {java_name} = '
                f'new ELispSymbol.Value.{t}();{suffix}'
            )
            inits.append(f'        {c_name}.initForwardTo({java_name});')
        assert stem not in variables, f'{stem} already defined'
        variables[stem] = f'''{'\n'.join(var_defs)}
    private static void {stem}Vars() {{
{'\n'.join(inits)}
    }}
'''
    all_inits = sorted(variables.items(), key=lambda kv: kv[0])
    inits = f'''
    public static void initGlobalVariables() {{
{'\n'.join(f'        {stem}Vars();' for stem, _ in all_inits)}
    }}
{'\n'.join(init for _, init in all_inits)}
'''
    with open(output_file, 'r') as f:
        contents = f.read()
    contents = replace_or_insert_region(contents, 'initGlobalVariables', inits)
    with open(output_file, 'w') as f:
        f.write(contents)


class PESerializer:
    '''Serializes a list of `PEValue`s into Java code.'''

    _local_vars: dict[str, str]

    symbol_mapping: dict[str, str]
    '''Maps Lisp symbol names to Java variable names.'''

    def __init__(self, extraction: EmacsExtraction, symbols: dict[str, str]):
        self.extraction = extraction
        self.symbol_mapping = symbols
        self.lisp_variables = {
            f.lisp_name: f
            for file in extraction.file_extractions
            for f in file.lisp_variables
        }
        self.lisp_functions = {
            f.lisp_name: f
            for file in extraction.file_extractions
            for f in file.functions
        }
        self._local_vars = {}

    def reset(self):
        self._local_vars = {}

    def _java_symbol(self, symbol: str) -> str:
        java_name = self.symbol_mapping.get(symbol)
        if java_name is not None:
            return java_name
        # ELispContext#intern(String)
        return f'intern({json.dumps(symbol)})'

    def _java_lisp_var(self, name: str) -> str:
        java_name = self._local_vars.get(name)
        if java_name is not None:
            # A initializing lisp variable with a local variable
            return java_name
        java_name = _c_name_to_java(self.symbol_mapping[name])
        return f'{java_name}.getValue()'

    def _java_arg_list(self, args: list[PECValue] | list[PEValue]) -> str:
        arg_list = []
        for arg in args:
            v = self._expr_to_java(arg)
            assert isinstance(v, str)
            arg_list.append(v)
        return ', '.join(arg_list)

    def _java_lisp_call(self, function: str, args: list[PEValue]) -> str:
        if function == 'define-coding-system-internal':
            # CALLMANY
            return f'defineCodingSystemInternal(/* TODO */)'
        match function:
            case 'list':
                return f'ELispCons.listOf({self._java_arg_list(args)})'
            case 'purecopy':
                assert len(args) == 1
                result = self._expr_to_java(args[0])
                assert isinstance(result, str)
                return result
        assert function in self.lisp_functions
        f = self.lisp_functions[function]
        assert f.c_name.startswith('F')
        function = f'F{_c_name_to_java_class(f.c_name[1:])}.{_c_name_to_java(f.c_name[1:])}'
        return f'{function}({self._java_arg_list(args)})'

    def _java_function_call(self, function: str, args: list[PECValue]) -> str:
        match function:
            case 'make_fixnum':
                assert len(args) == 1
                return f'(long) ({self._expr_to_java(args[0])})'
            case 'make_float':
                assert len(args) == 1
                return f'(double) ({self._expr_to_java(args[0])})'
            case 'make_string':
                assert len(args) == 2 and isinstance(args[1], int)
                return f'new ELispString({self._expr_to_java(args[0])})'
            case 'make_vector':
                assert len(args) == 2 and isinstance(args[0], int)
                return f'new ELispVector({args[0]}, {self._expr_to_java(self._boolean(args[1]))})'
            case 'make_symbol_constant':
                assert len(args) == 1
                return f'{self._expr_to_java(args[0])}.setConstant(true)'
            case 'make_symbol_special':
                assert len(args) == 2 and isinstance(args[1], bool)
                return f'{self._expr_to_java(args[0])}.setSpecial({self._expr_to_java(args[1])})'
            case 'init_buffer_local_defaults':
                return f'initBufferLocalDefaults(/* TODO */)'
            case 'define_charset_internal':
                return f'defineCharsetInternal({self._java_arg_list(args)})'
            case _:
                try:
                    return f'{function}({self._java_arg_list(args)}) /* TODO */'
                except Exception as e:
                    print(f'Error in {function}: {e}')
                    raise e

    def _boolean(self, value: PECValue) -> PECValue:
        if isinstance(value, PELispSymbol):
            if value.lisp_name == 't':
                value = True
            elif value.lisp_name == 'nil':
                value = False
        return value

    def _expr_to_java(self, pe_value: PECValue) -> str | list[str] | None:
        match pe_value:
            case bool(b):
                return 'true' if b else 'false'
            case int(i):
                return str(i)
            case float(f):
                return str(f)
            case str(s):
                return json.dumps(s)
            case PELispSymbol(name):
                return self._java_symbol(name)
            case PELispVariable(name):
                return self._java_lisp_var(name)
            case PECVariable(name, local):
                # TODO
                return _c_var_name_to_java(name)
            case PELispForm(function, args):
                return self._java_lisp_call(function, args)
            case PECFunctionCall(function, args):
                return self._java_function_call(function, args)
            case PELispVariableAssignment(name, value):
                local_var = self._local_vars.get(name)
                init = None
                if local_var is None:
                    if name not in self.lisp_variables:
                        return f'{self.symbol_mapping[name]}.setValue({self._expr_to_java(value)})'
                    java_name = _c_name_to_java(self.symbol_mapping[name])
                    local_var = f'{java_name}JInit'
                    self._local_vars[name] = local_var
                    init = f'{java_name}.setValue({local_var})'
                value = self._boolean(value)
                assign = f'{local_var} = {self._expr_to_java(value)}'
                return assign if init is None else [
                    f'var {assign}',
                    init,
                ]
            case PECVariableAssignment(name, value, local):
                if not local:
                    # TODO
                    return None
                if value is None:
                    return None
                local_var = self._local_vars.get(name)
                prefix = ''
                if local_var is None:
                    prefix = 'var '
                    local_var = _c_var_name_to_java(name)
                    self._local_vars[name] = local_var
                value = self._boolean(value)
                return f'{prefix}{local_var} = {self._expr_to_java(value)}'
            case builtins.list:
                pe_value
                pass
            case _:
                raise Exception(f'Unknown expression type: {pe_value}')

    def serialize(self, function: InitFunction):
        self.reset()
        results: list[str] = []
        for statement in function.statements:
            result = self._expr_to_java(statement)
            if result is not None:
                if isinstance(result, list):
                    results.extend(f'{line};' for line in result)
                else:
                    results.append(f'{result};')
        return results


def export_initializations(extraction: EmacsExtraction, symbols: dict[str, str], output_file: str):
    serializer = PESerializer(extraction, symbols)
    for init in extraction.initializations:
        function = init.name
        print(f'/*** {function} ***/')
        print('\n'.join(serializer.serialize(init)))


def finalize(extraction: EmacsExtraction):
    parser = argparse.ArgumentParser()
    parser.add_argument('-s', '--symbols', required=True, help='Symbol output java file')
    parser.add_argument('-g', '--globals', required=True, help='Variable output java file')
    args = parser.parse_args(get_unknown_cmd_flags())

    symbol_mapping = export_symbols(extraction, args.symbols)
    export_variables(extraction, symbol_mapping, args.globals)
    export_initializations(extraction, symbol_mapping, args.globals)


set_finalizer(finalize)
