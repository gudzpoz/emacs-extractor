import dataclasses
import re
import typing
from os import PathLike
from pathlib import Path

from tree_sitter import Query, Node

from emacs_extractor.constants import extract_define_constants, CConstant, extract_enum_constants
from emacs_extractor.subroutines import extract_subroutines, Subroutine
from emacs_extractor.utils import remove_all_includes, preprocess_c, parse_c, C_LANG, require_single, require_text
from emacs_extractor.variables import (
    extract_variables, extract_symbols,
    LispVariable, PerBufferVariable, CVariable,
    LispSymbol,
)


@dataclasses.dataclass
class FileContents:
    file: Path
    lisp_variables: list[LispVariable]
    per_buffer_variables: list[PerBufferVariable]
    c_variables: list[CVariable]
    constants: list[CConstant]
    functions: list[Subroutine]


_MAIN_FUNCTION_FILE = 'emacs.c'
_MAIN_FUNCTION_QUERY = Query(C_LANG, r'''
(function_definition
 (function_declarator
  (identifier) @main (#eq? @main "main")
 )
) @def
''')
_INIT_CALL_QUERY = Query(C_LANG, r'''
(expression_statement
 (call_expression
  (identifier) @init (#match? @init "^(init_|syms_of_)")
  (argument_list "(" . ")" )
 )
) @node
''')
_INIT_FUNCTION_DEF_QUERY = Query(C_LANG, r'''
(function_definition
 (function_declarator
  (identifier) @init (#match? @init "^(init_|syms_of_)")
 )
) @node
''')


@dataclasses.dataclass
class InitCall:
    line: int
    call: str
    comments: list[str]


class EmacsExtractor:
    """Extracts fields from Emacs Lisp files."""

    directory: PathLike[str] | str
    files: list[str]
    preprocessors: typing.Optional[str]
    init_calls: list[InitCall]

    def __init__(
            self,
            directory: PathLike[str] | str,
            files: list[str],
            preprocessors: typing.Optional[str] = None,
            extra_constants: typing.Optional[dict[str, typing.Any]] = None,
    ):
        self.directory = directory
        self.files = files
        self.preprocessors = preprocessors
        self.extra_constants = extra_constants or {}
        self.init_calls = self._extract_init_calls()

    def _extract_init_calls(self):
        with open(Path(self.directory).joinpath(_MAIN_FUNCTION_FILE), 'r') as f:
            source = f.read()
        tree = self._preprocess(source, 'emacs.c')
        _, match = require_single(_MAIN_FUNCTION_QUERY.matches(tree.root_node))
        body = require_single(match['def']).child_by_field_name('body')
        assert body is not None
        init_calls: list[tuple[int, typing.Literal['comment', 'init'], str]] = [] # (line, type, text)
        for _, match in _INIT_CALL_QUERY.matches(body):
            node = require_single(match['node'])
            prev = node.prev_sibling
            if prev is not None and prev.type == 'comment':
                init_calls.append((prev.start_point.row, 'comment', require_text(prev)))
            post = node.next_sibling
            if post is not None and post.type == 'comment':
                init_calls.append((post.start_point.row, 'comment', require_text(post)))
            init = require_text(match['init'])
            init_calls.append((node.start_point.row, 'init', init))
        init_calls = sorted(set(init_calls))
        comments: list[tuple[int, str]] = []
        calls: list[InitCall] = []
        for line, kind, text in init_calls:
            if kind == 'comment':
                comments.append((line, text))
            else:
                calls.append(InitCall(
                    line,
                    text,
                    [comment for l, comment in comments if l == line or l == line - 1]
                ))
                comments = []
        assert len(comments) == 0
        return calls

    def _preprocess(self, source: str, file: str):
        source = remove_all_includes(source)
        preprocessors = self.preprocessors
        if file.endswith('.h'):
            file = re.sub(r'\W', '_', file)
            preprocessors = f'''#define EXTRACTING_{file.upper()}\n{preprocessors}'''
        source = preprocess_c(source, preprocessors)
        tree = parse_c(source.encode())
        return tree

    def extract_static(self):
        global_constants: dict[str, typing.Any] = dict(self.extra_constants)
        files: list[FileContents] = []
        init_functions: dict[str, tuple[Node, FileContents]] = {}
        with open(Path(self.directory).joinpath('globals.h'), 'r') as f:
            all_symbols = extract_symbols(f.read())
        for file in self.files:
            path = Path(self.directory).joinpath(file)
            with open(path, 'r') as f:
                source = f.read()
                source = source.replace('#define DEFVAR_PER_BUFFER', '#define _DEFVAR_PER_BUFFER')

            tree = parse_c(source.encode())

            # Variables
            c, lisp, per_buffer = extract_variables(tree.root_node)

            # Subroutines
            functions = extract_subroutines(tree.root_node, global_constants)

            # #define constants
            define_constants, defined = extract_define_constants(tree.root_node, global_constants)

            tree = self._preprocess(source, file)

            # #define constants
            extract_define_constants(tree.root_node, global_constants, defined)

            # Enum constants
            enum_constants = extract_enum_constants(tree.root_node, global_constants)
            file_constants = enum_constants + define_constants
            if file.endswith('.h'):
                global_constants.update({c.name: c.value for c in file_constants})

            file_info = FileContents(
                file=path.relative_to('.'),
                lisp_variables=lisp,
                per_buffer_variables=per_buffer,
                c_variables=c,
                constants=file_constants,
                functions=functions,
            )
            files.append(file_info)

            init_functions.update(self._extract_init_functions(tree.root_node, file_info))
        return files, all_symbols, init_functions

    @classmethod
    def _extract_init_functions(cls, root: Node, file: FileContents):
        functions: dict[str, tuple[Node, FileContents]] = {}
        for _, match in _INIT_FUNCTION_DEF_QUERY.matches(root):
            name = require_text(match['init'])
            if name.startswith('init_') or name.startswith('syms_of_'):
                functions[name] = (require_single(match['node']), file)
        return functions
