from tree_sitter import Node, Query
from emacs_extractor.config import SpecificConfig
from emacs_extractor.partial_eval import PELispSymbol
from emacs_extractor.utils import C_LANG, require_single, require_text, trim_doc
from emacs_extractor.variables import LispSymbol


FRAME_PARMS_QUERY = Query(C_LANG, '''
(declaration
 (init_declarator
  (array_declarator) @name (#eq? @name "frame_parms[]")
  (initializer_list) @values
 )
)
''')


def _extract_symbol_index(node: Node, symbol_mapping: dict[str, LispSymbol]) -> LispSymbol:
    assert node.type == 'call_expression'
    assert require_text(node.child_by_field_name('function')) == 'SYMBOL_INDEX'
    c_name = require_text(node.child_by_field_name('arguments')).strip('()')
    return symbol_mapping[c_name]


def _extract_string_array(root: Node, var_name: str):
    query = Query(C_LANG, f'''
(declaration
 (init_declarator
  (pointer_declarator
   (array_declarator) @name (#eq? @name "{var_name}[]")
  )
  (initializer_list) @values
 )
)''')
    _, match = require_single(query.matches(root))
    strings = []
    for item in require_single(match['values']).named_children:
        if item.text == b'0':
            strings.append(False)
        else:
            assert item.type == 'string_literal'
            strings.append(eval(require_text(item)))
    return strings


def extract_frame_parms(
        configs: dict[str, SpecificConfig],
        root: Node,
        symbol_mapping: dict[str, LispSymbol]
):
    _, match = require_single(FRAME_PARMS_QUERY.matches(root))
    symbols = []
    for item in require_single(match['values']).named_children:
        assert item.type == 'initializer_list'
        assert len(item.named_children) == 2
        param_name, param_symbol = item.named_children
        assert param_name.type == 'string_literal'
        param_name = eval(require_text(param_name))
        if param_symbol.text != b'-1':
            assert param_name == _extract_symbol_index(param_symbol, symbol_mapping).lisp_name
        param_symbol = PELispSymbol(param_name)
        symbols.append(param_symbol)
    config = configs['syms_of_frame']
    if config.extra_globals is None:
        config.extra_globals = {}
    config.extra_globals['frame_parms'] = symbols


KEYBOARD_HEAD_TABLE_QUERY = Query(C_LANG, '''
(declaration
 (init_declarator
  (array_declarator) @name (#eq? @name "head_table[]")
  (initializer_list) @values
 )
)
''')
KEYBOARD_MODIFIER_NAMES_QUERY = Query(C_LANG, '''
''')


def extract_keyboard_c(
        configs: dict[str, SpecificConfig],
        root: Node,
        symbol_mapping: dict[str, LispSymbol]
):
    config = configs['syms_of_keyboard']
    if config.extra_globals is None:
        config.extra_globals = {}

    _, match = require_single(KEYBOARD_HEAD_TABLE_QUERY.matches(root))
    event_heads = []
    for item in require_single(match['values']).named_children:
        if item.type == 'comment':
            continue
        assert item.type == 'initializer_list'
        assert len(item.named_children) == 2
        event, event_kind = item.named_children
        event_symbol = _extract_symbol_index(event, symbol_mapping)
        event_kind_symbol = _extract_symbol_index(event_kind, symbol_mapping)
        event_heads.append((
            PELispSymbol(event_symbol.lisp_name),
            PELispSymbol(event_kind_symbol.lisp_name),
        ))
    config.extra_globals['head_table'] = event_heads

    config.extra_globals['modifier_names'] = _extract_string_array(root, 'modifier_names')
    config.extra_globals['lispy_wheel_names'] = _extract_string_array(root, 'lispy_wheel_names')


STRUCT_BUFFER_QUERY = Query(C_LANG, '''
(struct_specifier
 (type_identifier) @name (#eq? @name "buffer")
 (field_declaration_list) @fields
)
''')


def extract_struct_buffer(
        configs: dict[str, SpecificConfig],
        root: Node,
        symbol_mapping: dict[str, LispSymbol]
):
    _, match = require_single(STRUCT_BUFFER_QUERY.matches(root))
    buffer_fields = []
    last_comment, last_comment_line = None, -1
    for field in require_single(match['fields']).named_children:
        if field.type == 'comment':
            last_comment = trim_doc(require_text(field))
            last_comment_line = field.end_point.row
            continue
        assert field.type == 'field_declaration', field
        if require_text(field.child_by_field_name('type')) != 'Lisp_Object':
            continue
        field_name = require_text(field.child_by_field_name('declarator'))
        assert field_name.endswith('_')
        field_name = field_name[:-1]
        comment = last_comment if (
            field.start_point.row == last_comment_line
            or field.start_point.row == last_comment_line + 1
        ) else None
        buffer_fields.append((field_name, comment))
    config = configs['init_buffer_once']
    if config.extra_globals is None:
        config.extra_globals = {}
    config.extra_globals['struct_buffer_fields'] = buffer_fields
