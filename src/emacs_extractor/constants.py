import dataclasses
import typing

from tree_sitter import Node, Query

from emacs_extractor.utils import C_LANG, require_not_none, require_single, require_text


@dataclasses.dataclass
class CConstant:
    name: str
    value: int | str
    raw_value: str
    group: str | None = None


_DEFINE_CONSTANT_QUERY = Query(C_LANG, r'''
(preproc_def .
 (identifier) @name
 !parameters
 (preproc_arg) @value
)
''')


def extract_define_constants(
        root: Node,
        global_constants: dict[str, typing.Any],
        update: typing.Optional[dict[str, CConstant]] = None
):
    """
    Extracts #define constants from the given root node.
    """
    global_constants = dict(global_constants)
    constants: list[CConstant] = []
    defined: dict[str, CConstant] = update or {}
    for _, match in _DEFINE_CONSTANT_QUERY.matches(root):
        name = require_not_none(require_single(match['name']).text).decode()
        value = require_not_none(require_single(match['value']).text).decode()
        try:
            v = eval(value, global_constants)
            if isinstance(v, int) or isinstance(v, str):
                if name not in defined:
                    if update is None:
                        c = CConstant(name, v, value)
                        constants.append(c)
                        defined[name] = c
                else:
                    defined[name].value = v
                global_constants[name] = v
        except NameError:
            pass
        except SyntaxError:
            pass
    return constants, defined


_ENUM_CONSTANT_QUERY = Query(C_LANG, r'''
(enum_specifier
 (type_identifier)? @group
 (enumerator_list) @list
)
''')
_IGNORED_ENUMS = {
    # lisp.h
    'Lisp_Type',
    'NIL_IS_ZERO',
    'SUB_CHAR_TABLE_OFFSET',
    'USE_STACK_CONS',
    'USE_STACK_STRING',
    # alloc.c
    'roundup_size',
    # buffer.h
    'BUFFER_LISP_SIZE',
    'BUFFER_REST_SIZE',
    # composite.c
    'GLYPH_LEN_MAX',
    # editfns.c
    'USEFUL_PRECISION_MAX',
    # lread.c
    'word_size_log2',
}


def extract_enum_constants(root: Node, global_constants: dict[str, typing.Any]):
    """
    Extracts enum constants from the given root node.
    """
    global_constants = dict(global_constants)
    constants: list[CConstant] = []
    ignored: list[str] = []
    for _, match in _ENUM_CONSTANT_QUERY.matches(root):
        index = 0
        enum_list = match['list']
        group = None
        if 'group' in match and len(match['group']) != 0:
            group = require_text(match['group'])
            if group in _IGNORED_ENUMS:
                continue
        for enumerator in require_single(enum_list).named_children:
            if enumerator.type != 'enumerator':
                continue
            name_node = enumerator.child_by_field_name('name')
            assert name_node is not None, enum_list[0].text
            name = require_not_none(name_node.text).decode()
            if name in _IGNORED_ENUMS:
                continue
            value_node = enumerator.child_by_field_name('value')
            try:
                if name in global_constants:
                    value = global_constants[name]
                    if isinstance(value, str):
                        value = eval(value, global_constants)
                    assert isinstance(value, int)
                    index = value
                elif value_node is not None:
                    text = require_text(value_node)
                    if ('alignof' in text
                        or 'sizeof' in text
                        or 'offsetof' in text
                        or 'ROUNDUP' in text
                    ):
                        ignored.append(name)
                        continue
                    elif any(n in text for n in ignored):
                        ignored.append(name)
                        continue
                    value = eval(text, global_constants)
                    assert isinstance(value, int), f'{name}: {value}'
                    index = value
                constants.append(CConstant(name, index, str(index), group))
                global_constants[name] = index
                index += 1
            except Exception as e:
                raise SyntaxError(f'{name}: {e}')
    return constants
