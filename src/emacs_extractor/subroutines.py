import dataclasses
import re
import typing

from tree_sitter import Node, Query

from emacs_extractor.utils import C_LANG, parse_c, require_single, require_text, trim_doc


@dataclasses.dataclass
class Subroutine:
    lisp_name: str
    c_name: str
    symbol_c_name: str
    min_args: int
    max_args: int
    int_spec: str
    doc: str
    args: list[str]
    exported: bool = False


_DEFUN_QUERY = Query(C_LANG, r'''
(expression_statement
 (call_expression
  (call_expression
   (identifier) @macro (#eq? @macro "DEFUN")
   (argument_list
    (string_literal) @lisp_name ","
    (identifier) @c_name ","
    (identifier) @symbol_c_name "," .
    (_) @min_args "," .
    (_) @max_args "," .
    (_) @int_spec ","
    (identifier)? @doc_start (#eq? @doc_start "doc")
    (comment) @doc
   )
  )
  (argument_list) @args
 )
)
''')


_USAGE_PATTERN = re.compile(r'^usage: \(([ &.[\]0-9a-zA-Z_-]+)\)$', re.MULTILINE)
_PARSED_DECLARATION_QUERY = Query(C_LANG, r'''
(declaration
 (function_declarator .
  (identifier) .
  (parameter_list) @args
 )
)
''')


def extract_signature(args_node: Node, doc: str):
    usage_match = _USAGE_PATTERN.match(doc)
    if usage_match is not None:
        usage = usage_match.group(1)
        usage_args: list[str] = [
            arg.strip('[]')
            for arg in usage.replace('...', ' ').strip().split(' ')[1:]
            if arg != '&optional' and arg != '&rest'
        ]
        return usage_args

    declaration = f'void f {require_text(args_node)};'
    parsed = parse_c(declaration.encode())
    _, match = require_single(_PARSED_DECLARATION_QUERY.matches(parsed.root_node))
    args = [arg for arg in require_single(match['args']).children if arg.type == 'parameter_declaration']
    is_void = False
    is_var_args = False
    arg_names: list[str] = []
    for i, arg in enumerate(args):
        type_node = arg.child_by_field_name('type')
        assert type_node is not None, (declaration, require_text(arg))
        type_name = require_text(type_node)
        if type_name == 'void':
            is_void = True
        elif type_name == 'Lisp_Object':
            declarator = arg
            while declarator.type != 'identifier':
                declarator = declarator.child_by_field_name('declarator')
                assert declarator is not None
            arg_names.append(require_text(declarator))
        else:
            assert type_name == 'ptrdiff_t' and i == 0
            is_var_args = True
    if is_void:
        assert len(arg_names) == 0 and len(args) == 1
    if is_var_args:
        assert len(arg_names) == 1 and len(args) == 2
    return arg_names


_DEFSUBR_QUERY = Query(C_LANG, r'''
(expression_statement
 (call_expression
  (identifier) @defsubr (#eq? @defsubr "defsubr")
  (argument_list
   (pointer_expression (identifier) @symbol_c_name)
  )
 )
)
''')


def extract_subroutines(root: Node, global_variables: dict[str, typing.Any]):
    mapping: dict[str, Subroutine] = {}
    subroutines: list[Subroutine] = []
    for _, match in _DEFUN_QUERY.matches(root):
        lisp_name = eval(require_text(match['lisp_name']))
        c_name = require_text(match['c_name'])
        symbol_c_name = require_text(match['symbol_c_name'])
        min_args = eval(require_text(match['min_args']), global_variables)
        max_args = eval(require_text(match['max_args']), global_variables)
        int_spec = eval(require_text(match['int_spec']), global_variables)
        doc = require_text(match['doc'])
        doc = trim_doc(doc)
        args_node = require_single(match['args'])
        args = extract_signature(args_node, doc)
        subr = Subroutine(lisp_name, c_name, symbol_c_name, min_args, max_args, int_spec, doc, args)
        mapping[symbol_c_name] = subr
        subroutines.append(subr)
    for _, match in _DEFSUBR_QUERY.matches(root):
        symbol_c_name = require_text(match['symbol_c_name'])
        assert symbol_c_name in mapping, symbol_c_name
        subr = mapping[symbol_c_name]
        subr.exported = True
    return subroutines
