import re
import subprocess
import textwrap
import typing

import tree_sitter_c as ts_c
from tree_sitter import Language, Parser, Node, TreeCursor

T = typing.TypeVar('T')


def require_single(l: list[T]) -> T:
    assert len(l) == 1, l
    return l[0]


def require_not_none(l: T | None) -> T:
    assert l is not None, l
    return l


def require_text(node: Node | None | list[Node]) -> str:
    if isinstance(node, list):
        node = require_single(node)
    assert node is not None, node
    text = node.text
    assert text is not None, node
    return text.decode()


def trim_doc(doc: str) -> str:
    doc = doc.strip()
    if doc.startswith("/*"):
        doc = doc[2:]
    if doc.endswith("*/"):
        doc = doc[:-2]
    first_line_i = doc.find('\n')
    if first_line_i != -1:
        first_line = doc[:first_line_i]
        doc = f'{first_line}\n{textwrap.dedent(doc[first_line_i+1:])}'
    return doc.strip()


C_LANG = Language(ts_c.language())


def parse_c(source: bytes):
    parser = Parser(C_LANG)
    tree = parser.parse(source)
    return tree


def get_declarator(node: Node):
    while True:
        declarator = node.child_by_field_name('declarator')
        if declarator is None:
            return node
        node = declarator


def _goto_parent_sibling(cursor: TreeCursor, root: Node):
    while True:
        if cursor.node == root or not cursor.goto_parent():
            return False
        if cursor.goto_next_sibling():
            return True


def tree_walker(tree: Node | None, callback: typing.Callable[[Node], bool]):
    """
    Walks the tree and calls the callback for each node.
    The callback should return False if the walk should skip the children of the node.

    However, one is recommended to use tree-sitter Query instead of this function.
    """
    assert tree is not None, tree
    if not callback(tree):
        return
    if len(tree.children) == 0:
        return
    cursor = tree.walk()
    while True:
        if not cursor.goto_first_child() and not cursor.goto_next_sibling():
            if not _goto_parent_sibling(cursor, tree):
                return
        node = cursor.node
        while node is not None:
            if callback(node):
                break
            if cursor.goto_next_sibling():
                node = cursor.node
                continue
            if not _goto_parent_sibling(cursor, tree):
                return
            node = cursor.node


_space = ord(' ')


def remove_all_includes(source: str):
    encoded_source = source.encode()
    tree = parse_c(encoded_source)
    source_bytes = bytearray(encoded_source)

    def remove_include(node: Node):
        nonlocal source_bytes
        if node.type == 'preproc_include':
            for i in range(node.start_byte, node.end_byte):
                source_bytes[i] = _space
            return False
        return True
    tree_walker(tree.root_node, remove_include)

    return source_bytes.decode()


_PREPROCESSOR_REMAINS = re.compile(r'^#.*$', flags=re.MULTILINE)


def preprocess_c(source: str, extra_preprocessors: typing.Optional[str] = None):
    """
    Runs gcc -E and returns the result.
    """
    if extra_preprocessors is not None:
        source = extra_preprocessors + '\n' + source
    p = subprocess.Popen(
        ['gcc', '-E', '-C', '-Wp,-dD', '-'],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE
    )
    stdout, stderr = p.communicate(source.encode())
    assert stderr is None, stderr
    processed = stdout.decode()
    return _PREPROCESSOR_REMAINS.sub('', processed)
