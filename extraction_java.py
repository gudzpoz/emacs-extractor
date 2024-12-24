import argparse
import json
import re

from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from xml.sax.saxutils import escape as escape_xml

from pyparsing import (
    alphanums,
    LineStart,
    Literal,
    QuotedString,
    SkipTo,
    Word,
)

from emacs_extractor.config import (
    EmacsExtraction, InitFunction, FileContents,
    get_unknown_cmd_flags, set_finalizer,
)
from emacs_extractor.partial_eval import *
from emacs_extractor.utils import dataclass_deep_to_json


if TYPE_CHECKING:
    from extraction_config import BufferLocalProperty


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
    '''Generates initialization code for lisp symbols.'''
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
        for var in file.lisp_variables + file.per_kboard_variables:
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

def _javadoc(docstring: str, pre: bool = False):
    lines = escape_xml(docstring.replace('\t', '        ')).splitlines()
    doc_lines = ['     * <pre>'] if pre else []
    for line in lines:
        if line.strip() == '':
            doc_lines.append('     *')
        else:
            doc_lines.append(f'     * {line}')
    if pre:
        doc_lines.append('     * </pre>')
    return f'/**\n{'\n'.join(doc_lines)}\n     */'

VAR_ARG_FUNCTIONS = {
    'make-hash-table',
    'nconc',
}
ALLOWED_GLOBAL_C_VARS = {
    'cached_system_name': None,
    'gstring_work_headers': None,
    'regular_top_level_message': None,
    'Vcoding_category_table': None,
}


def export_variables(extraction: EmacsExtraction, constants: dict[str, str], symbols: dict[str, str], output_file: str):
    '''Generates definitions of forwarded lisp variables.'''
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
            init = ''
            match var.lisp_type:
                case 'INT':
                    t = 'ForwardedLong'
                    init_value = var.init_value
                    if init_value is not None:
                        if isinstance(init_value, PEIntConstant):
                            init = constants.get(init_value.c_name, f'{init_value.value}')
                        else:
                            init = f'{init_value}'
                case 'BOOL':
                    t = 'ForwardedBool'
                    if var.init_value is not None:
                        init = f'{'true' if var.init_value else 'false'}'
                case 'LISP':
                    t = 'Forwarded'
                    match var.init_value:
                        case bool(sym):
                            init = f'{'true' if sym else 'false'}'
                        case int(i):
                            init = f'{i}L'
                        case PEIntConstant(name, value):
                            init = constants.get(name, f'{value}L')
                        case float(f):
                            init = f'{f}'
                        case str(s):
                            init = f'new ELispString({json.dumps(s)})'
                        case _:
                            assert var.init_value is None, var.init_value
            var_defs.append(
                f'    private final ValueStorage.{t} {java_name} = '
                f'new ValueStorage.{t}({init});{suffix}'
            )
            inits.append(f'        initForwardTo({c_name}, {java_name});')
        assert stem not in variables, f'{stem} already defined'
        variables[stem] = f'''{'\n'.join(var_defs)}
    private void {stem}Vars() {{
{'\n'.join(inits)}
    }}'''
    all_inits = sorted(variables.items(), key=lambda kv: kv[0])
    inits = f'''    public void initGlobalVariables() {{
{'\n'.join(f'        {stem}Vars();' for stem, _ in all_inits)}
    }}
{'\n'.join(init for _, init in all_inits)}
'''
    with open(output_file, 'r') as f:
        contents = f.read()
    contents = replace_or_insert_region(contents, 'initGlobalVariables', inits)
    with open(output_file, 'w') as f:
        f.write(contents)


CONSTANT_GROUPS = {
    # lisp.h
    'CHARTAB_SIZE_BITS',
    'Lisp_Closure',
    'char_bits',
    # character.h
    'NO_BREAK_SPACE',
    'UNICODE_CATEGORY_UNKNOWN',
    # charset.h
    'define_charset_arg_index',
    'charset_attr_index',
    # coding.h
    'define_coding_system_arg_index',
    'define_coding_iso2022_arg_index',
    'define_coding_utf8_arg_index',
    'define_coding_utf16_arg_index',
    'define_coding_ccl_arg_index',
    'define_coding_undecided_arg_index',
    'coding_attr_index',
    'coding_result_code',
    # syntax.h
    'syntaxcode',
    # coding.c
    'coding_category',
}
CONSTANT_REGEXPS = {
    'lisp.h': ['CHAR_TABLE_STANDARD_SLOTS', 'MAX_CHAR', 'MAX_UNICODE_CHAR'],
    'character.h': ['MAX_._BYTE_CHAR'],
    'coding.h': [r'CODING_\w+_MASK'],
    'coding.c': [r'CODING_ISO_FLAG_\w+'],
}


def export_constants(extraction: EmacsExtraction, symbols: dict[str, str], output_file: str):
    '''Exports extracted constants.'''
    with open(output_file, 'r') as f:
        contents = f.read()

    java_symbols = set(symbols.values())
    encoded: dict[str, str] = {}
    for file in extraction.file_extractions:
        groups: list[str] = []
        group_constants: dict[str, list[CConstant]] = {}
        regexps = [re.compile(regexp) for regexp in CONSTANT_REGEXPS.get(file.file.name, [])]
        region = []
        for constant in file.constants:
            if constant.group in CONSTANT_GROUPS:
                group = constant.group
            else:
                for reg in regexps:
                    if reg.fullmatch(constant.name):
                        group = reg.pattern
                        break
                else:
                    continue
            if group not in groups:
                groups.append(group)
                group_constants[group] = []
            group_constants[group].append(constant)
        for group in groups:
            region = []
            for constant in group_constants[group]:
                java_name = constant.name.upper()
                if java_name in java_symbols:
                    java_name += '_INIT'
                encoded[constant.name] = java_name
                assert isinstance(constant.value, int)
                value = f'{constant.raw_value or constant.value}'
                value = ' '.join(encoded.get(s, s) for s in value.split(' '))
                region.append(f'public static final int {java_name} = {value};')
            contents = replace_or_insert_region(
                contents, group,
                f'    {'\n    '.join(region)}\n',
            )
    with open(output_file, 'w') as f:
        f.write(contents)
    return encoded


class PESerializer:
    '''Serializes a list of `PEValue`s into Java code.'''

    _local_vars: dict[str, str]

    buffer_local_properties: tuple[list['BufferLocalProperty'], list[tuple[str, str | None]]]
    frame_fields: list[tuple[str, str | None]]
    kboard_fields: list[tuple[str, str | None]]

    symbol_mapping: dict[str, str]
    '''Maps Lisp symbol names to Java variable names.'''

    _arg_list_indent: int

    def __init__(self, extraction: EmacsExtraction, constants: dict[str, str], symbols: dict[str, str]):
        self.extraction = extraction
        self.constants = constants
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
        self.buffer_local_properties = ([], [])
        self.frame_fields = []
        self.kboard_fields = []
        self._local_vars = {}
        self._arg_list_indent = 12

    def reset(self):
        self._local_vars = {}
        self._arg_list_indent = 12

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

    def _java_arg_list(self, args: list[PECValue] | list[PEValue], joiner: str = ', ') -> str:
        arg_list = []
        indent = len(args) > 6 and joiner == ', '
        if indent:
            self._arg_list_indent += 4
        for arg in args:
            v = self._expr_to_java(arg)
            assert isinstance(v, str)
            arg_list.append(v)
        if indent:
            self._arg_list_indent -= 4
            spaces = ' ' * self._arg_list_indent
            joiner = f',\n{spaces}'
        text = joiner.join(arg_list)
        if indent:
            text = f'\n{spaces}{text}\n{' ' * (self._arg_list_indent - 4)}'
        return text

    def _java_lisp_call(self, function: str, args: list[PEValue]) -> str:
        # Things should still work without the following match-case, but
        # it serves to simplify the code and make it more OOP.
        match function:
            case 'set':
                assert len(args) == 2
                symbol = self._expr_to_java(args[0])
                assert isinstance(symbol, str)
                if '.' in symbol:
                    # Complex operation
                    symbol = f'asSym({symbol})'
                return f'{symbol}.setValue({self._expr_to_java(args[1])})'
            case 'aref':
                assert len(args) == 2
                return f'{self._expr_to_java(args[0])}.get({self._expr_to_java(args[1])})'
            case 'aset':
                assert len(args) == 3
                return f'{self._expr_to_java(args[0])}.set({self._expr_to_java(args[1])}, {self._expr_to_java(args[2])})'
            case 'list':
                return f'ELispCons.listOf({self._java_arg_list(args)})'
            case 'purecopy':
                assert len(args) == 1
                result = self._expr_to_java(args[0])
                assert isinstance(result, str)
                return result
            case 'current-buffer':
                assert len(args) == 0
                return 'currentBuffer()'
            case 'make-variable-buffer-local':
                assert len(args) == 1
                return f'{self._expr_to_java(args[0])}.setBufferLocal(true)'
            case 'internal-make-var-non-special':
                assert len(args) == 1
                return f'{self._expr_to_java(args[0])}.setSpecial(false)'
            case 'unintern':
                assert len(args) == 2
                assert self._expr_to_java(args[1]) == 'NIL'
                return f'unintern({self._expr_to_java(args[0])})'
            case 'define-coding-system-internal':
                return f'defineCodingSystemInternal(new Object[]{{{self._java_arg_list(
                    [arg or False for arg in args]
                )}}})'
        assert function in self.lisp_functions
        f = self.lisp_functions[function]
        assert f.c_name.startswith('F')
        if function in VAR_ARG_FUNCTIONS:
            arg_list = f'new Object[]{{{self._java_arg_list(args)}}}'
        else:
            arg_list = self._java_arg_list(args)
        return f'F{_c_name_to_java_class(f.c_name[1:])}.{_c_name_to_java(f.c_name[1:])}({arg_list})'

    def _java_function_call(self, function: str, args: list[PECValue]) -> str | None:
        # Need to implement the functions in PE_C_FUNCTIONS as well as
        # PECFunctionCall in extraction_config.py.
        match function:
            case 'make_fixnum' | 'make_int':
                assert len(args) == 1
                return f'(long) ({self._expr_to_java(args[0])})'
            case 'make_float':
                assert len(args) == 1
                return f'(double) ({self._expr_to_java(args[0])})'
            case 'make_string':
                assert len(args) == 2 and isinstance(args[1], PEInt)
                return f'new ELispString({self._expr_to_java(args[0])})'
            case 'make_vector':
                assert len(args) == 2 and isinstance(args[0], PEInt)
                if isinstance(args[0], PEIntConstant):
                    length = self.constants.get(args[0].c_name, args[0].value)
                else:
                    length = f'{args[0]}'
                return f'''new ELispVector({length}, {
                    self._expr_to_java(self._boolean(args[1]))
                })'''
            case 'make_symbol_constant':
                assert len(args) == 1
                return f'{self._expr_to_java(args[0])}.setConstant(true)'
            case 'make_symbol_special':
                assert len(args) == 2 and isinstance(args[1], bool)
                return f'{self._expr_to_java(args[0])}.setSpecial({self._expr_to_java(args[1])})'
            case 'set_char_table_purpose':
                assert len(args) == 2
                return f'{self._expr_to_java(args[0])}.setPurpose({self._expr_to_java(args[1])})'
            case 'set_char_table_defalt':
                assert len(args) == 2
                return f'{self._expr_to_java(args[0])}.setDefault({self._expr_to_java(args[1])})'
            case 'char_table_set':
                assert len(args) == 3
                return f'{self._expr_to_java(args[0])}.set({
                    self._expr_to_java(args[1])
                }, {self._expr_to_java(args[2])})'
            case 'char_table_set_range':
                assert len(args) == 4
                return f'{self._expr_to_java(args[0])}.setRange({
                    self._expr_to_java(args[1])
                }, {self._expr_to_java(args[2])}, {
                    self._expr_to_java(args[3])
                })'
            case 'decode_env_path':
                assert len(args) == 3
                assert isinstance(args[2], int)
                if args[0] == 0:
                    args[0] = PELiteral('null')
                args[2] = args[2] != 0
                return f'decodeEnvPath({self._java_arg_list(args)})'
            case 'init_frame_fields':
                assert len(args) == 1
                assert all(isinstance(arg, tuple) for arg in cast(Any, args[0]))
                self.frame_fields = cast(Any, args[0])
                return None
            case 'init_kboard_fields':
                assert len(args) == 1
                assert all(isinstance(arg, tuple) for arg in cast(Any, args[0]))
                self.kboard_fields = cast(Any, args[0])
                return 'ELispKboard.initKboardLocalVars(ctx)'
            case 'init_buffer_local_defaults':
                assert len(args) == 2
                assert all(is_dataclass(arg) for arg in cast(Any, args[0]))
                assert all(isinstance(arg, tuple) for arg in cast(Any, args[1]))
                self.buffer_local_properties = tuple(cast(Any, args))
                for var in self.buffer_local_properties[0]:
                    # I am too lazy to create yet another dataclass...
                    if var.default is not None:
                        expr = self._expr_to_java(var.default)
                        assert isinstance(expr, str)
                        var.default = expr
                return None
            case 'init_buffer_once':
                assert len(args) == 0
                return 'ELispBuffer.initBufferLocalVars(ctx, bufferDefaults)'
            case 'init_buffer_directory':
                assert len(args) == 2
                assert args[0] == 'current_buffer'
                assert args[1] == 'minibuffer_0'
                return 'ELispBuffer.initDirectory()'
            case 'set_buffer_default_category_table':
                assert len(args) == 1
                return f'bufferDefaults.setCategoryTable({self._expr_to_java(args[0])})'
            case 'set_and_check_load_path':
                assert len(args) == 0
                return 'setAndCheckLoadPath()'
            case 'init_dynlib_suffixes':
                assert len(args) == 0
                return 'initDynlibSuffixes()'
            case 'get_minibuffer':
                return f'getMiniBuffer({self._java_arg_list(args)})'
            case 'define_charset_internal':
                return f'builtInCharSet.defineCharsetInternal({self._java_arg_list([PELiteral('this')] + args)})'
            case 'setup_coding_system':
                assert len(args) == 2
                assert self._expr_to_java(args[1]) == 'safeTerminalCoding'
                return f'''builtInCoding.setupCodingSystem({
                    self._expr_to_java(args[0])
                }, builtInCoding.safeTerminalCoding)'''
            case 'allocate_kboard':
                # TODO
                return 'false /* TODO */'
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
                if '\n' in s:
                    return f'''"""\n{'\n'.join(
                        json.dumps(line)[1:-1] for line in s.split('\n')
                    )}"""'''
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
                if name not in self.lisp_variables:
                    return f'{self.symbol_mapping[name]}.setValue({self._expr_to_java(value)})'
                local_var = self._local_vars.get(name)
                java_name = _c_name_to_java(self.symbol_mapping[name])
                if local_var is None:
                    local_var = f'{java_name}JInit'
                else:
                    _, tail = local_var.split('JInit')
                    if tail == '':
                        tail = '1'
                    else:
                        tail = str(int(tail) + 1)
                    local_var = f'{java_name}JInit{tail}'
                value = self._boolean(value)
                assign = f'{local_var} = {self._expr_to_java(value)}'
                init = f'{java_name}.setValue({local_var})'
                self._local_vars[name] = local_var
                return assign if init is None else [
                    f'var {assign}',
                    init,
                ]
            case PECVariableAssignment(name, value, local):
                if not local and name not in ALLOWED_GLOBAL_C_VARS:
                    return None
                if value is None:
                    return None
                local_var = self._local_vars.get(name)
                prefix = ''
                if local_var is None:
                    if local or (ALLOWED_GLOBAL_C_VARS[name] is None):
                        prefix = 'var '
                    local_var = _c_var_name_to_java(name)
                    self._local_vars[name] = local_var
                value = self._boolean(value)
                return f'{prefix}{local_var} = {self._expr_to_java(value)}'
            case PELiteral(value):
                return value
            case PEIntConstant(name, value):
                if name in self.constants:
                    return self.constants[name]
                return self._expr_to_java(value)
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


MANUALLY_IMPLEMENTED = {
    'init_casetab_once': 'builtInCaseTab.initCaseTabOnce(ctx)',
    'init_charset': 'initCharset()',
    'init_syntax_once': 'builtInSyntax.initSyntaxOnce(ctx)',
}


def export_initializations(
        extraction: EmacsExtraction,
        constants: dict[str, str],
        symbols: dict[str, str],
        output_file: str,
):
    '''Generates initialization code for PE AST and buffer-local definitions.'''
    serializer = PESerializer(extraction, constants, symbols)
    initializations = []
    calls = []
    for init in extraction.initializations:
        function = init.name
        java_function = _c_name_to_java(function)
        serialized = serializer.serialize(init)
        if len(serialized) == 0:
            if function in MANUALLY_IMPLEMENTED:
                calls.append(f'        {MANUALLY_IMPLEMENTED[function]};')
            continue
        if function == 'syms_of_editfns':
            # Prune prin1_to_string_buffer
            assert serialized[0] == 'var obuf = currentBuffer();'
            assert serialized[1] == 'FSetBuffer.setBuffer(prin1ToStringBuffer);'
            assert serialized[3] == 'FSetBuffer.setBuffer(obuf);'
            serialized = serialized[4:]
        calls.append(f'        {java_function}();')
        initializations.append(f'''
    private void {java_function}() {{
        {'\n        '.join(serialized)}
    }}''')
    region = f'''    public void postInitVariables() {{
{'\n'.join(calls)}
    }}
{''.join(initializations)}
'''
    with open(output_file, 'r') as f:
        contents = f.read()
    with open(output_file, 'w') as f:
        contents = replace_or_insert_region(
            contents,
            'initializations',
            region,
        )
        f.write(contents)
    return serializer


def export_forwardable_locals(
        output_file: str,
        fields: list[tuple[str, str | None]],
        array_field: str,
        const_prefix: str,
        tag: str,
        extra: str = '',
):
    with open(output_file, 'r') as f:
        contents = f.read()
    buffer_region = f'''    private final Object[] {array_field};
{extra}    {'\n    '.join(
        f'''{'' if comment is None
           else f'{_javadoc(comment)}\n    '
        }public final static int {const_prefix}{name.upper()} = {i};'''
        for i, (name, comment) in enumerate(fields)
    )}
    {'\n    '.join(
        f'''public Object get{_c_name_to_java_class(name)}() {{ return {array_field}[{const_prefix}{
        name.upper()
    }]; }}
    public void set{_c_name_to_java_class(name)}(Object value) {{ {array_field}[{const_prefix}{
        name.upper()
    }] = value; }}'''
        for name, _ in fields
    )}
'''
    contents = replace_or_insert_region(
        contents,
        tag,
        buffer_region,
    )
    return contents


def export_buffer_locals(
        extraction: EmacsExtraction,
        symbols: dict[str, str],
        serializer: PESerializer,
        buffer_output_file: str,
):
    properties, fields = serializer.buffer_local_properties
    contents = export_forwardable_locals(
        buffer_output_file, fields,
        'bufferLocalFields',
        'BVAR_',
        'struct buffer',
        f'private static final byte[] BUFFER_LOCAL_FLAGS = new byte[{len(fields)}];\n',
    )
    buffer_fields = set(field for field, _ in fields)
    buffer_locals = {
        var.c_name: var
        for file in extraction.file_extractions
        for var in file.per_buffer_variables
    }
    buffer_properties = {
        prop.name: prop
        for prop in properties
    }
    assert all(name in buffer_fields for name in buffer_locals.keys())
    assert all(name in buffer_fields for name in buffer_properties.keys())
    lines = []
    for field, _ in fields:
        if field in buffer_properties:
            prop = buffer_properties[field]
            if prop.default == 'NIL':
                continue
            lines.append(f'''defaultValues.set{_c_name_to_java_class(field)}({
                prop.default
            });''')
    for field, _ in fields:
        if field in buffer_properties:
            if buffer_properties[field].permanent_local:
                lines.append(f'''BUFFER_LOCAL_FLAGS[BVAR_{
                    field.upper()
                }] = Byte.MIN_VALUE; // PERMANENT_LOCAL''')
            else:
                lines.append(f'''BUFFER_LOCAL_FLAGS[BVAR_{field.upper()}] = {
                    buffer_properties[field].local_flag
                };''')
    for field, _ in fields:
        if field in buffer_locals:
            var = buffer_locals[field]
            java_name = symbols[var.lisp_name]
            predicate = var.predicate
            assert predicate.startswith('Q')
            predicate = predicate[1:].upper()
            lines.append(f'''context.forwardTo({java_name}, new ValueStorage.ForwardedPerBuffer(BVAR_{
                field.upper()
            }, {predicate}));''')
    contents = replace_or_insert_region(
        contents,
        'init_buffer_once',
        f'        {'\n        '.join(lines)}\n',
        indents=8,
    )

    with open(buffer_output_file, 'w') as f:
        f.write(contents)


def export_frame_fields(
        serializer: PESerializer,
        frame_output_file: str,
):
    with open(frame_output_file, 'r') as f:
        contents = f.read()
    frame_fields = serializer.frame_fields
    lines = []
    for field, comment in frame_fields:
        if comment is not None:
            lines.append(f'    {_javadoc(comment, pre=True)}')
        name = _c_name_to_java(field)
        method = _c_name_to_java_class(field)
        lines.append(f'    private Object {name} = false;')
        lines.append(f'    public Object get{method}() {{ return {name}; }}')
        lines.append(f'    public void set{method}(Object value) {{ {name} = value; }}')
    contents = replace_or_insert_region(
        contents,
        'struct frame',
        f'{'\n'.join(lines)}\n',
    )
    with open(frame_output_file, 'w') as f:
        f.write(contents)


def export_kboard_fields(
        extraction: EmacsExtraction,
        serializer: PESerializer,
        symbols: dict[str, str],
        kboard_output_file: str,
):
    fields = serializer.kboard_fields
    contents = export_forwardable_locals(
        kboard_output_file, fields,
        'kboardLocalFields',
        'KVAR_',
        'struct kboard',
    )
    kboard_locals = {
        var.c_name: var
        for file in extraction.file_extractions
        for var in file.per_kboard_variables
    }
    lines = []
    for field, _ in fields:
        if field in kboard_locals:
            var = kboard_locals[field]
            java_name = symbols[var.lisp_name]
            lines.append(f'''context.forwardTo({java_name}, new ValueStorage.ForwardedPerKboard(KVAR_{
                field.upper()
            }));''')
    contents = replace_or_insert_region(
        contents,
        'init_kboard_once',
        f'        {'\n        '.join(lines)}\n',
        indents=8,
    )
    with open(kboard_output_file, 'w') as f:
        f.write(contents)


JAVA_NODE_DETECT = re.compile(
    r'public abstract static class (\w+) extends ELispBuiltInBaseNode',
    re.MULTILINE | re.DOTALL,
)
JAVA_NODE_MATCH = (
    LineStart()
    + Literal('@ELispBuiltIn(name =')
    + QuotedString('"')('name')
    + SkipTo(')')('attrs') + ')'
    + Literal('@GenerateNodeFactory')
    + Literal('public abstract static class')
    + Word('F', alphanums)('fname')
    + SkipTo('{')('extends') + '{'
    + SkipTo('\n    }\n')('body')
)


NON_VAR_ARG_SPECIAL_FORMS = {
    'defconst': 3,
    'defvar': 3,
    'function': 1,
    'quote': 1,
}


def generate_subroutine_attrs(subroutine: Subroutine):
    upper = max(0, subroutine.min_args) if subroutine.max_args < 0 else subroutine.max_args
    is_varargs = False
    match subroutine.max_args:
        case -2: # MANY
            is_varargs = True
            varargs = ', varArgs = true'
            if subroutine.min_args > 10:
                subroutine.min_args = 0
            upper = max(0, subroutine.min_args)
        case -1 if subroutine.lisp_name not in NON_VAR_ARG_SPECIAL_FORMS: # UNEVALLED
            is_varargs = True
            varargs = ', varArgs = true'
            varargs = ', varArgs = true'
            upper = max(0, subroutine.min_args)
        case -1:
            upper = NON_VAR_ARG_SPECIAL_FORMS[subroutine.lisp_name]
            varargs = ', varArgs = false'
        case _:
            varargs = ''
            upper = subroutine.max_args
    assert upper >= 0 and subroutine.min_args >= 0
    return f''', minArgs = {subroutine.min_args}, maxArgs = {upper}{varargs}{
        ', rawArg = true' if subroutine.max_args == -1 else ''
    }''', (upper, is_varargs)


EXISTING_SPECIALIZATION_PATTERN = (
    LineStart()
    + (
        Literal('@Specialization\n') |
        (Literal('@Specialization(') + SkipTo('\n'))
    )
    + SkipTo('{')('line')
)
THIS_USAGE = re.compile(r'\b(this|getContext|getLanguage|getStorage|getFunctionStorage)\b')


def export_subroutines_in_file(extraction: FileContents, output: Path):
    with open(output, 'r') as f:
        contents = f.read()
    existing = dict(
        (m['fname'], m)
        for m in JAVA_NODE_MATCH.search_string(contents)
    )

    start = contents.find('\n    @ELispBuiltIn(name =')
    if start == -1:
        original = contents[0:contents.rindex('}')]
    else:
        original = contents[0:start]
        comment_end = original.rfind('*/')
        if original[comment_end:].strip() == '*/':
            trailing_comment_start = original.rfind('\n    /**\n')
            assert trailing_comment_start != -1
            original = original[0:trailing_comment_start]
    for subroutine in extraction.functions:
        assert subroutine.c_name.startswith('F')
        fname = f'F{_c_name_to_java_class(subroutine.c_name[1:])}'
        attrs, (max_args, is_varargs) = generate_subroutine_attrs(subroutine)
        if is_varargs:
            if len(subroutine.args) == max_args:
                subroutine.args.append('args')
            assert max_args + int(is_varargs) <= len(subroutine.args), subroutine
            if max_args + int(is_varargs) < len(subroutine.args):
                subroutine.args = subroutine.args[:max_args + int(is_varargs)]
                subroutine.args[-1] = 'args'
        else:
            assert max_args == len(subroutine.args), (max_args, subroutine)
        subroutine.args = [_c_name_to_java(arg) for arg in subroutine.args]
        javadoc = _javadoc(subroutine.doc, True)
        if fname in existing:
            info = existing[fname]
            assert info['name'] == subroutine.lisp_name
            assert info['attrs'] == attrs, (subroutine, info['attrs'], attrs)
            extends = info['extends']
            body = info['body']
            assert (
                extends == 'extends ELispBuiltInBaseNode '
                or extends == 'extends ELispBuiltInBaseNode '
                'implements ELispBuiltInBaseNode.InlineFactory '
            )
            assert '@Specialization' in body
            impls = EXISTING_SPECIALIZATION_PATTERN.search_string(body)
            assert len(impls) >= 1, body
            for impl in impls:
                line = str(impl['line']).strip()
                if not line.startswith('public static'):
                    assert THIS_USAGE.search(body) is not None, line
                    assert line.startswith('public '), line
                line = line[line.index('(') + 1:line.rindex(')')]
                if '@Cached' in line:
                    line = line.split('@Cached')[0]
                if 'VirtualFrame frame' in line:
                    line = line.replace('VirtualFrame frame', '')
                assert '@' not in line, line
                actual_args = [arg.strip().split()[-1] for arg in line.split(',') if arg.strip() != '']
                assert actual_args == subroutine.args, (actual_args, subroutine)
        else:
            extends = 'extends ELispBuiltInBaseNode '
            args = []
            for i, arg in enumerate(subroutine.args):
                if is_varargs and i == len(subroutine.args) - 1:
                    args.append(f'Object[] {arg}')
                else:
                    args.append(f'Object {arg}')
            body = f'''@Specialization
        public static Void {_c_name_to_java(subroutine.c_name[1:])}({', '.join(args)}) {{
            throw new UnsupportedOperationException();
        }}'''
        original += f'''
    {javadoc}
    @ELispBuiltIn(name = "{subroutine.lisp_name}"{attrs})
    @GenerateNodeFactory
    public abstract static class {fname} {extends}{{
        {body}
    }}
'''
    original += '}\n'
    with open(output, 'w') as f:
        f.write(original)


def export_subroutines(extraction: EmacsExtraction, output_dir: str):
    '''Generates boilerplate for definitions of Emacs built-in subroutines.'''
    outputs = {
        output.stem[len('BuiltIn'):].lower(): output
        for output in Path(output_dir).glob('*.java')
        if output.stem.startswith('BuiltIn')
    }
    for file in extraction.file_extractions:
        if file.file.name.endswith('.h'):
            continue
        assert file.file.stem.lower() in outputs, file.file.stem
        output = outputs[file.file.stem.lower()]
        export_subroutines_in_file(file, output)


def finalize(extraction: EmacsExtraction):
    parser = argparse.ArgumentParser()
    parser.add_argument('-C', '--constants', required=True, help='Constant output java file')
    parser.add_argument('-g', '--globals', required=True, help='Variable output java file')
    parser.add_argument('-d', '--builtin-dir', required=True, help='Directory for java classes of subroutines')
    parser.add_argument('--buffer', required=True, help='Buffer init output java file')
    parser.add_argument('--frame', required=True, help='Frame init output java file')
    parser.add_argument('--kboard', required=True, help='Kboard init output java file')
    args = parser.parse_args(get_unknown_cmd_flags())

    symbol_mapping = export_symbols(extraction, args.globals)
    constants = export_constants(extraction, symbol_mapping, args.constants)
    export_variables(extraction, constants, symbol_mapping, args.globals)
    serializer = export_initializations(extraction, constants, symbol_mapping, args.globals)
    export_buffer_locals(extraction, symbol_mapping, serializer, args.buffer)
    export_frame_fields(serializer, args.frame)
    export_kboard_fields(extraction, serializer, symbol_mapping, args.kboard)
    export_subroutines(extraction, args.builtin_dir)


set_finalizer(finalize)
