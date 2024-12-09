from tree_sitter import Node, Query
from emacs_extractor.config import SpecificConfig
from emacs_extractor.partial_eval import PECFunctionCall, PELispSymbol
from emacs_extractor.utils import C_LANG, require_single, require_text


FRAME_PARMS_QUERY = Query(C_LANG, '''
(declaration
 (init_declarator
  (array_declarator) @name (#eq? @name "frame_parms[]")
  (initializer_list) @values
 )
)
''')


def extract_frame_parms(config: SpecificConfig, root: Node):
    _, match = require_single(FRAME_PARMS_QUERY.matches(root))
    symbols = []
    for item in require_single(match['values']).named_children:
        assert item.type == 'initializer_list'
        assert len(item.named_children) == 2
        param_name, param_symbol = item.named_children
        assert param_name.type == 'string_literal'
        param_name = eval(require_text(param_name))
        if param_symbol.text != b'-1':
            assert param_symbol.type == 'call_expression'
            assert require_text(param_symbol.child_by_field_name('function')) == 'SYMBOL_INDEX'
        param_symbol = PELispSymbol(param_name)
        symbols.append(param_symbol)
    if config.extra_globals is None:
        config.extra_globals = {}
    config.extra_globals['frame_parms'] = symbols
