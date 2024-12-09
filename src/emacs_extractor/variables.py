import dataclasses
import re
import typing

from tree_sitter import Node, Query

from emacs_extractor.constants import extract_define_constants
from emacs_extractor.utils import C_LANG, get_declarator, parse_c, require_not_none, require_single, require_text


LISP_VAR_TYPES = typing.Literal['BOOL', 'INT', 'LISP', 'KBOARD']
_DEFVAR_KINDS: dict[str, LISP_VAR_TYPES | typing.Literal['PER_BUFFER']] = {
    'DEFVAR_BOOL': 'BOOL',
    'DEFVAR_INT': 'INT',
    'DEFVAR_LISP': 'LISP',
    'DEFVAR_LISP_NOPRO': 'LISP', # no staticpro, not relevant to us
    'DEFVAR_KBOARD': 'KBOARD',
    'DEFVAR_PER_BUFFER': 'PER_BUFFER',
}


@dataclasses.dataclass
class CVariable:
    c_name: str
    static: bool


@dataclasses.dataclass
class LispVariable:
    lisp_name: str
    c_name: str
    lisp_type: LISP_VAR_TYPES
    init_value: typing.Any


@dataclasses.dataclass
class PerBufferVariable:
    lisp_name: str
    c_name: str
    predicate: str


def is_declaration(node: Node):
    return node.type == 'declaration'


def is_static(node: Node):
    return any(
        specifier.text == 'static' for specifier in node.children
        if specifier.type == 'storage_class_specifier'
    )


def is_type(node: Node, type_name: str):
    return require_text(node.child_by_field_name('type')) == type_name


def get_identifiers(node: Node) -> list[str]:
    return [
        require_not_none(identifier.text).decode()
        for identifier in node.children
        if identifier.type == 'identifier'
    ]


_DEFVAR_MATCH_QUERY = Query(C_LANG, r'''
(expression_statement .
 (call_expression .
  (identifier) @macro (#match? @macro "^DEFVAR_[A-Z_]+$")
 )
) @node
''')
_DEFVAR_GLOBAL_CAPTURE_QUERY = Query(C_LANG, r'''
(expression_statement
 (call_expression
  (identifier) @macro
  (argument_list
   (string_literal) @lisp_name ","
   (identifier) @c_name ","
   (identifier) @doc_start (#eq? @doc_start "doc")
   (comment) @doc
  )
 )
)
''')
_DEFVAR_PER_BUFFER_CAPTURE_QUERY = Query(C_LANG, r'''
(expression_statement
 (call_expression
  (identifier) @macro
  (argument_list
   (string_literal) @lisp_name ","
   (pointer_expression
    (call_expression
     (identifier) @bvar_macro (#eq? @bvar_macro "BVAR")
     (argument_list "("
      (identifier) @current_buffer (#eq? @current_buffer "current_buffer") ","
      (identifier) @c_name ")"
     )
    )
   ) ","
   (identifier) @predicate ","
   (identifier) @doc_start (#eq? @doc_start "doc")
   (comment) @doc
  )
 )
)
''')


def extract_variables(root_node: Node):
    """
    Extracts variables from the root node.

    This includes global C Lisp_Object variables, Lisp variables, and per-buffer variables.
    Please note that this function treats only top-level variables as global ones. And
    since declarations wrapped in #if directives are not included in the root node,
    the caller is responsible for first preprocessing the source code.
    """
    c_variables: list[CVariable] = []
    for node in root_node.children:
        if is_declaration(node) and is_type(node, 'Lisp_Object'):
            static = is_static(node)
            for identifier in get_identifiers(node):
                c_variables.append(CVariable(
                    c_name=identifier,
                    static=static,
                ))
    buffer_locals: list[PerBufferVariable] = []
    lisp_variables: list[LispVariable] = []
    for _, match in _DEFVAR_MATCH_QUERY.matches(root_node):
        nodes = match['node']
        macros = match['macro']
        assert len(nodes) == 1 and len(macros) == 1
        node = nodes[0]
        macro = require_not_none(macros[0].text).decode()
        assert macro in _DEFVAR_KINDS, macro
        kind = _DEFVAR_KINDS[macro]
        if kind == 'PER_BUFFER':
            matches = _DEFVAR_PER_BUFFER_CAPTURE_QUERY.matches(node)
            assert len(matches) == 1, node.text
            _, match = matches[0]
            buffer_locals.append(PerBufferVariable(
                lisp_name=eval(require_text(match['lisp_name'])),
                c_name=require_text(match['c_name']),
                predicate=require_text(match['predicate']),
            ))
        else:
            matches = _DEFVAR_GLOBAL_CAPTURE_QUERY.matches(node)
            assert len(matches) == 1, require_text(node)
            _, match = matches[0]
            kind = typing.cast(LISP_VAR_TYPES, kind)
            lisp_variables.append(LispVariable(
                lisp_name=eval(require_text(match['lisp_name'])),
                c_name=require_text(match['c_name']),
                lisp_type=kind,
                init_value=None,
            ))
    return c_variables, lisp_variables, buffer_locals


@dataclasses.dataclass
class LispSymbol:
    lisp_name: str
    c_name: str


_DEFSYM_PATTERN = re.compile(r'^DEFINE_LISP_SYMBOL \(([A-Za-z0-9_]+)\)$', re.MULTILINE)
_DEFSYM_NAME_QUERY = Query(C_LANG, r'''
(init_declarator) @node
''')


def extract_symbols(globals_h: str) -> list[LispSymbol]:
    globals_h = globals_h[globals_h.index('#define iQnil 0'):]

    globals_h_root_node = parse_c(globals_h.encode()).root_node
    symbol_c_names = _DEFSYM_PATTERN.findall(globals_h)
    symbol_lisp_names = []
    for _, match in _DEFSYM_NAME_QUERY.matches(globals_h_root_node):
        if require_text(get_declarator(require_single(match['node']))) == 'defsym_name':
            name_list = require_single(match['node']).child_by_field_name('value')
            assert name_list is not None and name_list.type == 'initializer_list'
            for name_node in name_list.named_children:
                assert name_node.type == 'string_literal'
                symbol_lisp_names.append(eval(require_text(name_node)))
            break
    else:
        raise RuntimeError('defsym_name not found')

    constants, _ = extract_define_constants(globals_h_root_node, {})
    constant_dict = { constant.name: constant for constant in constants }
    assert len(symbol_c_names) == len(symbol_lisp_names)
    symbols = []
    for i, (c_name, lisp_name) in enumerate(zip(symbol_c_names, symbol_lisp_names)):
        assert f'i{c_name}' in constant_dict
        assert i == constant_dict[f'i{c_name}'].value
        symbols.append(LispSymbol(lisp_name=lisp_name, c_name=c_name))
    return symbols
