import re
from tree_sitter import Node

from emacs_extractor.config import SpecificConfig
from emacs_extractor.extractor import FileContents
from emacs_extractor.utils import require_not_none, require_single, require_text, tree_walker, trim_doc


class CTranspiler:
    _result_stack: list[list[str]]
    _indentations: list[int]
    _replaces_stack: list[list[tuple[re.Pattern, str | None]]]

    def __init__(
            self,
            init_functions: dict[str, tuple[Node, FileContents]],
            ignored_patterns: dict[str, SpecificConfig],
            ignored_functions: set[str],
    ) -> None:
        self.init_functions = init_functions
        self.ignored_patterns = {
            file: config.transpile_replaces
            for file, config in ignored_patterns.items()
            if config.transpile_replaces is not None
        }
        self.ignored_functions = ignored_functions
        self._result_stack = []
        self._indentations = [0]
        self._replaces_stack = []

    def _transpile_expression(self, expression: Node) -> str:
        string_builder: list[bytes] = []
        def walk_expression(node: Node):
            assert node.text is not None
            match node.type:
                case 'comment':
                    return False
                case 'conditional_expression':
                    string_builder.append(b'((')
                    tree_walker(node.child_by_field_name('consequence'), walk_expression)
                    string_builder.append(b') if (')
                    tree_walker(node.child_by_field_name('condition'), walk_expression)
                    string_builder.append(b') else (')
                    tree_walker(node.child_by_field_name('alternative'), walk_expression)
                    string_builder.append(b'))')
                    return False
                case 'declaration':
                    declarators = node.children_by_field_name('declarator')
                    for declarator in declarators:
                        size = None
                        value = declarator.child_by_field_name('value') if declarator.type == 'init_declarator' else None
                        while declarator is not None and declarator.type != 'identifier':
                            if declarator.type == 'array_declarator':
                                size = declarator.child_by_field_name('size')
                            declarator = declarator.child_by_field_name('declarator')
                        assert declarator is not None
                        tree_walker(declarator, walk_expression)
                        string_builder.append(b'=')
                        if size is not None:
                            string_builder.append(b'c_array(')
                            tree_walker(size, walk_expression)
                            string_builder.append(b',')
                            if value is not None:
                                tree_walker(value, walk_expression)
                            else:
                                string_builder.append(b'None')
                            string_builder.append(b')')
                        elif value is None:
                            string_builder.append(b'None')
                        else:
                            tree_walker(value, walk_expression)
                    return False
                case 'initializer_list':
                    string_builder.append(b'[')
                    for child in node.named_children:
                        tree_walker(child, walk_expression)
                        string_builder.append(b',')
                    string_builder.append(b']')
                    return False
                case 'pointer_expression':
                    operator = node.child_by_field_name('operator')
                    assert operator is not None
                    assert operator.text == b'*' or operator.text == b'&'
                    string_builder.append(b'c_pointer("' + operator.text + b'",')
                    tree_walker(node.child_by_field_name('argument'), walk_expression)
                    string_builder.append(b')')
                    return False
                case 'cast_expression':
                    tree_walker(node.child_by_field_name('type'), walk_expression)
                    string_builder.append(b'(')
                    tree_walker(node.child_by_field_name('value'), walk_expression)
                    string_builder.append(b')')
                    return False
                case 'sizeof_expression':
                    string_builder.append(b'sizeof(')
                    tree_walker(node.child_by_field_name('value'), walk_expression)
                    string_builder.append(b')')
                    return False
                case 'string_literal':
                    string_builder.append(node.text)
                    return False
                case 'char_literal':
                    string_builder.extend((b'ord(', node.text, b')'))
                    return False
                case 'update_expression':
                    assert node.child_count == 2
                    first, second = node.children
                    if first.text == b'++': # ++i
                        op = '+='
                        postfix = b''
                        value = second
                    elif first.text == b'--': # --i
                        op = '-='
                        postfix = b''
                        value = second
                    elif second.text == b'++': # i++
                        op = '+='
                        postfix = b' - 1'
                        value = first
                    elif second.text == b'--': # i--
                        op = '-='
                        postfix = b' + 1'
                        value = first
                    else:
                        raise NotImplementedError()
                    assert value.type == 'identifier'
                    self._result_stack[-1].append(self._indent(
                        f'{require_text(value)} {op} 1',
                    )[0])
                    string_builder.extend((require_not_none(value.text), postfix))
                    return False
                case 'assignment_expression':
                    self._indentations.append(0)
                    left = self._transpile_expression(
                        require_not_none(node.child_by_field_name('left')),
                    )
                    statement = f'{left}={self._transpile_expression(
                        require_not_none(node.child_by_field_name('right')),
                    )}'
                    replaced = self._try_replace(statement)
                    self._indentations.pop()
                    self._result_stack[-1].append(replaced)
                    if replaced == statement:
                        string_builder.append(left.encode())
                    return False
                case 'call_expression':
                    function_name = require_text(node.child_by_field_name('function'))
                    if function_name in self.init_functions and require_text(node.child_by_field_name('arguments')) == '()':
                        string_builder.append(self.transpile_to_python(function_name).encode())
                        return False
                case 'unary_expression':
                    operator = node.child_by_field_name('operator')
                    assert operator is not None
                    if operator.text == b'!':
                        string_builder.append(b'not ')
                    else:
                        string_builder.append(require_not_none(operator.text))
                    tree_walker(node.child_by_field_name('argument'), walk_expression)
                    return False
                case 'binary_expression':
                    operator = node.child_by_field_name('operator')
                    assert operator is not None
                    tree_walker(node.child_by_field_name('left'), walk_expression)
                    op = require_not_none(operator.text)
                    if op == b'&&':
                        op = b' and '
                    elif op == b'||':
                        op = b' or '
                    string_builder.append(op)
                    tree_walker(node.child_by_field_name('right'), walk_expression)
                    return False
            if len(node.children) == 0:
                string_builder.append(node.text)
            return True
        tree_walker(expression, walk_expression)
        return b''.join(string_builder).decode().replace(';', '')

    def transpile_to_python(self, named_function: str) -> str:
        if named_function in self.ignored_functions:
            return f'# TODO: `{named_function}` manual implementation needed'
        replaces: list[tuple[re.Pattern, str | None]] = []
        for pattern in self.ignored_patterns.get(named_function, []):
            if isinstance(pattern, str):
                pattern = (pattern, None)
            replaces.append((re.compile(pattern[0]), pattern[1]))
        self._replaces_stack.append(replaces)
        transpiled = self._transpile_to_python(self.init_functions[named_function][0])
        return '\n'.join(transpiled)

    def _transpile_to_python(
            self,
            function: Node,
    ) -> list[str]:
        results: list[str]
        if function.type == 'function_definition':
            sig = require_text(function.child_by_field_name('declarator'))
            sig = sig.replace('\n', ' ')
            results = [f'### {sig} ###']
            code = function.child_by_field_name('body')
        else:
            results = []
            code = function
        self._result_stack.append(results)

        assert code is not None
        if code.type == 'compound_statement':
            children = code.children
        else:
            children = [code]

        for child in children:
            value: str
            match child.type:
                case 'preproc_call':
                    continue
                case '{' | '}' | ';':
                    continue
                case 'compound_statement':
                    results.extend(self._transpile_to_python(child))
                    continue
                case 'if_statement':
                    results.append(f'if ({self._try_replace(self._transpile_expression(
                        require_not_none(child.child_by_field_name('condition'))
                    ))}):')
                    self._indentations.append(4)
                    then = require_not_none(child.child_by_field_name('consequence'))
                    results.extend(self._indent(self._transpile_to_python(then)))
                    alternate = child.child_by_field_name('alternative')
                    if alternate is not None:
                        assert alternate.type == 'else_clause'
                        results.append('else:')
                        results.extend(self._indent(self._transpile_to_python(
                            require_single(alternate.named_children),
                        )))
                    self._indentations.pop()
                    continue
                case 'while_statement':
                    condition = require_not_none(child.child_by_field_name('condition'))
                    results.append(f'while ({self._transpile_expression(condition)}):')
                    self._indentations.append(4)
                    body = require_not_none(child.child_by_field_name('body'))
                    results.extend(self._indent(self._transpile_to_python(body)))
                    self._indentations.pop()
                    continue
                case 'for_statement':
                    init = child.child_by_field_name('initializer')
                    if init is not None:
                        results.append(self._transpile_expression(init))
                    condition = child.child_by_field_name('condition')
                    if condition is not None:
                        results.append(f'while ({self._transpile_expression(condition)}):')
                    else:
                        results.append('while True:')
                    self._indentations.append(4)
                    body = require_not_none(child.child_by_field_name('body'))
                    results.extend(self._indent(self._transpile_to_python(body)))
                    update = child.child_by_field_name('update')
                    if update is not None:
                        results.extend(self._indent(self._transpile_expression(update)))
                    self._indentations.pop()
                    continue
                case 'enum_specifier':
                    for child in require_not_none(child.child_by_field_name('body')).named_children:
                        results.append(f'{
                            require_text(child.child_by_field_name('name'))
                        } = {
                            self._transpile_expression(
                                require_not_none(child.child_by_field_name('value')),
                            )
                        }')
                    continue
                case 'expression_statement':
                    if len(child.children) == 1:
                        assert child.children[0].type == ';', child.children
                        continue
                    assert len(child.children) == 2 and child.children[1].type == ';', child.children
                    value = self._transpile_expression(child.children[0])
                case 'comment':
                    doc = trim_doc(require_text(child))
                    value = f'# {doc.replace('\n', '\n# ')}'
                case 'declaration':
                    value = self._transpile_expression(child)
                case _:
                    raise NotImplementedError(f'Unknown node type: {child.type}: {require_text(child)}')
            results.append(self._try_replace(value))
        self._result_stack.pop()
        return results

    def _try_replace(self, value: str) -> str:
        for r, sub in self._replaces_stack[-1]:
            if r.search(value) is not None:
                if sub is None:
                    value = f'# {value}'
                else:
                    value = r.sub(sub, value)
                break
        return value

    def _indent(self, text: list[str] | str) -> list[str]:
        if isinstance(text, str):
            text = [text]
        indent = ' ' * self._indentations[-1]
        return ['\n'.join(f'{indent}{l}' for l in line.split('\n')) for line in text]
