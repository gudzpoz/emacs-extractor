import typing

from emacs_extractor import misc
from emacs_extractor.config import (
    EmacsExtractorConfig,
    SpecificConfig,
    set_config,
    get_emacs_dir,
)
from emacs_extractor.partial_eval import (
    PECFunctionCall, PECVariable, PELispVariable,
    PELispVariableAssignment,
)


#################
### Constants ###
#################

# emacs.c
SYSTEM_TYPE = 'gnu/linux' # the `system-type` variable
SYSTEM_CONFIGURATION = 'x86_64-pc-linux-gnu' # the `system-configuration` variable
PATH_DUMPLOADSEARCH = '' # segment of `source-directory`, supplied to decode_env_path
SYSTEM_CONFIG_OPTIONS = '' # the `system-configuration-options` variable
SYSTEM_CONFIG_FEATURES = '' # the `system-configuration-features` variable
PATH_SEPARATOR_CHAR = ':' # the `path-separator` variable
EMACS_COPYRIGHT = 'Copyright (C) 2024 Free Software Foundation, Inc.' # `emacs-copyright`

def extract_emacs_version():
    configure_ac = get_emacs_dir().parent.joinpath('configure.ac')
    assert configure_ac.exists()
    with configure_ac.open('r') as f:
        for line in f:
            if line.startswith('AC_INIT'):
                break
        else:
            raise RuntimeError('Could not find AC_INIT in configure.ac')
    if ')' not in line:
        line += ')'
    line = line.replace('[', '\'').replace(']', '\'')
    return eval(line, {
        'AC_INIT': lambda *args: args[1],
    })

EMACS_VERSION = extract_emacs_version() # `emacs-version`
REPORT_BUG_ADDRESS = '' # `report-emacs-bug-address`

# callproc.c
PATH_INFO = '/usr/share/info' # `configure-info-directory`
PATH_DATA = f'/usr/share/emacs/{EMACS_VERSION}/etc' # `data-directory`
PATH_DOC = PATH_DATA # `doc-directory`
PATH_EXEC = f'/usr/lib/emacs/{EMACS_VERSION}/{SYSTEM_CONFIGURATION}' # `exec-directory`

# data.c
MOST_POSITIVE_FIXNUM = 0x7fffffff # `most-positive-fixnum`
MOST_NEGATIVE_FIXNUM = -MOST_POSITIVE_FIXNUM - 1 # `most-negative-fixnum`

#############
### Files ###
#############

# The files should be ordered by first listing headers and then sources.
extracted_files = [
    'lisp.h',
    'buffer.h',
    'category.h',
    'character.h',
    'coding.h',
    'composite.h',
    'dispextern.h',
    'puresize.h',
    'syntax.h',
    '../lib/timespec.h',

    'alloc.c',
    'buffer.c',
    'callint.c',
    'callproc.c',
    'casefiddle.c',
    'casetab.c',
    'category.c',
    'ccl.c',
    'character.c',
    'chartab.c',
    'cmds.c',
    'coding.c',
    'comp.c',
    'composite.c',
    'data.c',
    'doc.c',
    'editfns.c',
    'emacs.c',
    'eval.c',
    'fileio.c',
    'floatfns.c',
    'fns.c',
    'frame.c',
    'keyboard.c',
    'keymap.c',
    'lread.c',
    'macros.c',
    'minibuf.c',
    'print.c',
    'process.c',
    'search.c',
    'syntax.c',
    'textprop.c',
    'timefns.c',
    'window.c',
    'xdisp.c',
    'xfaces.c',
]

#######################
### Configs & Hacks ###
#######################

class InitBufferOnceBuffer(dict):
    def __init__(self, buffer: str):
        super().__init__({ '__var__': buffer })
        self.own_text = None
        self.text = None
class InitBufferOnceGlobals(dict):
    def __init__(self):
        super().__init__({
            'buffer_permanent_local_flags': InitBufferOnceBuffer('buffer_permanent_local_flags'),
            'buffer_local_flags': InitBufferOnceBuffer('buffer_local_flags'),
            'buffer_defaults': InitBufferOnceBuffer('buffer_defaults'),
            'buffer_local_symbols': InitBufferOnceBuffer('buffer_local_symbols'),
            'reset_buffer': lambda buffer: PECFunctionCall(
                'reset_buffer', [buffer['__var__']],
            ),
            'reset_buffer_local_variables': lambda buffer, permanent: PECFunctionCall(
                'reset_buffer_local_variables', [buffer['__var__'], permanent],
            ),
            'set_buffer_intervals': lambda buffer, intervals: PECFunctionCall(
                'set_buffer_intervals', [buffer['__var__'], intervals],
            ),
        })
    def __contains__(self, key: str) -> bool:
        return super().__contains__(key) or key.startswith('bset_')
    def __missing__(self, key: str):
        if key.startswith('bset_'):
            field = key[5:]
            def set_buffer(buffer: dict[str, typing.Any], value: typing.Any):
                buffer[field] = value
            return set_buffer
        raise KeyError(key)

file_specific_configs = {
    # alloc.c
    'syms_of_alloc': SpecificConfig(
        transpile_replaces=[
            # Emacs watches these GC variables to adjust GC behavior,
            # but I guess we don't need that.
            r'Swatch_gc_cons_percentage',
            r'Swatch_gc_cons_threshold',
            r'Fadd_variable_watcher',
        ],
    ),
    # buffer.c
    'init_buffer_once': SpecificConfig(
        # init_buffer_once initializes buffer-local variables,
        # including the corresponding symbols, defaults, and flags.
        extra_globals=InitBufferOnceGlobals(),
    ),
    'init_buffer': SpecificConfig(
        # init_buffer mainly creates a scratch buffer and sets its directory,
        # which should be done at runtime. So here we replace most of it with no-op
        # and `init_buffer_directory` to let the implementer decide what to do.
        transpile_replaces=[
            (r'pwd=emacs_wd', 'pwd=False'),
            r'get_minibuffer\(0\)',
            r'bset_directory',
            (r'.+enable_multibyte_characters.+', r'False'),
        ],
        extra_globals={
            'stderr': None,
            'errno': None,
            'emacs_strerror': lambda _: None,
            'fprintf': lambda *_args: PECFunctionCall(
                'init_buffer_directory', ['current_buffer','minibuffer_0',],
            ),
        },
    ),
    # callproc.c
    'syms_of_callproc': SpecificConfig(
        extra_globals={
            'PATH_INFO': PATH_INFO,
        },
    ),
    'init_callproc_1': SpecificConfig(
        extra_globals={
            'PATH_DATA': PATH_DATA,
            'PATH_DOC': PATH_DOC,
            'PATH_EXEC': PATH_EXEC,
        },
    ),
    # coding.c
    'syms_of_coding': SpecificConfig(
        transpile_replaces=[
            r'memclear\(args,sizeof\(args\)\)',
            r'reset_coding_after_pdumper_load',
        ],
        extra_globals={
            'setup_coding_system': lambda *args: PECFunctionCall(
                'setup_coding_system', [*args],
            ),
            'safe_terminal_coding': PECVariable('safe_terminal_coding', False),
        },
    ),
    # data.c
    'syms_of_data': SpecificConfig(
        extra_globals={
            'MOST_POSITIVE_FIXNUM': MOST_POSITIVE_FIXNUM,
            'MOST_NEGATIVE_FIXNUM': MOST_NEGATIVE_FIXNUM,
        },
    ),
    # emacs.c
    'syms_of_emacs': SpecificConfig(
        extra_globals={
            'SYSTEM_TYPE': SYSTEM_TYPE,
            'EMACS_CONFIGURATION': SYSTEM_CONFIGURATION,
            'EMACS_CONFIG_OPTIONS': SYSTEM_CONFIG_OPTIONS,
            'EMACS_CONFIG_FEATURES': SYSTEM_CONFIG_FEATURES,
            'SEPCHAR': PATH_SEPARATOR_CHAR,
            'emacs_copyright': EMACS_COPYRIGHT,
            'emacs_version': EMACS_VERSION,
            'emacs_bugreport': REPORT_BUG_ADDRESS,
        },
    ),
    # frame.c
    'syms_of_frame': SpecificConfig(
        transpile_replaces=[
            (
                r'^v=.+intern_c_string.+frame_parms.+builtin_lisp_symbol.+$',
                r'v=frame_parms[i]',
            ),
        ],
        extra_extraction=misc.extract_frame_parms,
    ),
    # keyboard.c
    'syms_of_keyboard': SpecificConfig(
        transpile_replaces=[
            r'Vwhile_no_input_ignore_events=###',
            (r'=builtin_lisp_symbol\(p->var\)', r'=p[0]'),
            (r'=builtin_lisp_symbol\(p->kind\)', r'=p[1]'),
            (r'^(.+)->u.s.declared_special=False', r'make_symbol_special(\1, False)'),
            (r'lossage_limit', r'3 * MIN_NUM_RECENT_KEYS'),
        ],
        extra_extraction=misc.extract_keyboard_c,
        extra_globals={
            'XSYMBOL': lambda name: name,
            'allocate_kboard': lambda t: PECFunctionCall('allocate_kboard', [t]),
        },
    ),
    'init_while_no_input_ignore_events': SpecificConfig(
        transpile_replaces=[
            (r'^return (\w+)$', r'Vwhile_no_input_ignore_events=\1'),
        ],
    ),
    # lread.c
    'init_obarray_once': SpecificConfig(
        # init_obarray_once initializes the global obarray and the two special symbols:
        # `t` and `nil`.
        transpile_replaces=[
            (r'^(.+)->u.s.declared_special=True', r'make_symbol_special(\1, True)'),
            r'Vobarray',
            r'lispsym',
        ],
        extra_globals={
            'define_symbol': lambda _name, _value: None,
            'builtin_lisp_symbol': lambda _name: None,
            'defsym_name': [None] * 10000,
            'XBARE_SYMBOL': lambda name: name,
            'SET_SYMBOL_VAL': lambda name, value: PELispVariableAssignment(
                typing.cast(PELispVariable, name).lisp_name, value,
            ),
        },
    ),
    'init_lread': SpecificConfig(
        # init_lread initializes `load-path` from `EMACSLOADPATH`
        # and sets values for some global variables.
        # Again, `load-path` should be initialized at runtime and
        # we replace the initialization with no-op and `set_and_check_load_path`.
        transpile_replaces=[
            (r'use_loadpath and egetenv', 'False and '),
            r'load_path_check',
        ],
        extra_globals={
            'load_path_default': lambda: PECFunctionCall(
                'set_and_check_load_path', [],
            ),
            # will_dump_p: True is used to skip an if node
            'will_dump_p': lambda: True,
        },
    ),
    'syms_of_lread': SpecificConfig(
        transpile_replaces=[
            (r'^(.+)->u.s.declared_special=False', r'make_symbol_special(\1, False)'),
            r'DYNAMIC_LIB_SECONDARY_SUFFIX',
            (
                r'^(Vdynamic_library_suffixes)=.+DYNAMIC_LIB_SUFFIX.+',
                r'\1=init_dynlib_suffixes()',
            ),
        ],
        extra_globals={
            'init_dynlib_suffixes': lambda: PECFunctionCall(
                'init_dynlib_suffixes', [],
            ),
            'XBARE_SYMBOL': lambda name: name,
            'PATH_DUMPLOADSEARCH': PATH_DUMPLOADSEARCH,
        },
    ),
    # minibuf.c
    'init_minibuf_once': SpecificConfig(
        extra_globals={
            'get_minibuffer': lambda x: PECFunctionCall('get_minibuffer', [x]),
        },
    ),
    # timefns.c
    'syms_of_timefns': SpecificConfig(
        transpile_replaces=[
            r'flt_radix_power',
        ],
    ),
    # xdisp.c
    'syms_of_xdisp': SpecificConfig(
        transpile_replaces=[
            r'^echo_buffer',
            r'^echo_area_buffer',
        ]
    ),
    # xfaces.c
    'syms_of_xfaces': SpecificConfig(
        extra_globals={
            'hashtest_eq': PECVariable('hashtest_eq', False),
        },
    ),
}

set_config(
    EmacsExtractorConfig(
        files=extracted_files,
        extra_macros=r'''
// Basic constants
#define GNUC_PREREQ(a, b, c) 0
#define INT_MAX 0x7FFFFFFF
#define INTPTR_MAX INT_MAX
#define PTRDIFF_MAX INT_MAX
#define SIZE_MAX INT_MAX
#define INT_WIDTH 64
#define UINT_WIDTH 64
#define true True
#define false False

// `lisp.h`
#ifndef EXTRACTING_LISP_H
#define DEFVAR_BOOL(a, b, c) ;
#define DEFVAR_INT(a, b, c) ;
#define DEFVAR_LISP(a, b, c) ;
#define DEFVAR_LISP_NOPRO(a, b, c) ;
#define DEFVAR_PER_BUFFER(a, b, c, d) ;
#define DEFVAR_KBOARD(a, b, c) ;
#define DEFSYM(a, b) ;
#define defsubr(a) ;
#define eassert(a) ;
#define XSETFASTINT(a, b) (a) = (b);
#define AUTO_STRING(name, str) (name) = build_unibyte_string (str);
#define IEEE_FLOATING_POINT 1

#define XSETINT(a, b) (a) = make_fixnum(b);
#endif

#define PDUMPER_REMEMBER_SCALAR(a) ;
#define PDUMPER_IGNORE(a) ;
#define PDUMPER_RESET(a, b) ;
#define PDUMPER_RESET_LV(a, b) ;
#define pdumper_remember_lv_ptr_raw(a, b) ;
#define pdumper_do_now_and_after_load(a) a()
#define pdumper_do_now_and_after_late_load(a) a()

#define static_assert(a) ;
#define staticpro(a) ;

// This will affect how tree-sitter parses the code.
#define INLINE_HEADER_BEGIN ;
#define _GL_INLINE_HEADER_BEGIN ;

// buffer.c
#ifndef EXTRACTING_BUFFER_H
#define BVAR(a, b) (a)[#b]
#endif
#define BUFFER_PVEC_INIT(a) ;

// category.c
#define MAKE_CATEGORY_SET (Fmake_bool_vector (make_fixnum (128), Qnil))

// commands.h
#define Ctl(c) ((c)&037)

// keyboard.c
#if defined EXTRACTING_KEYBOARD_C
// Avoid an #error in keyboard.c
#define CYGWIN
#endif

// timefns.c
#if defined EXTRACTING_TIMEFNS_C
#define FIXNUM_OVERFLOW_P(a) 0
#endif
''',
        extra_extraction_constants={
            # Necessary for `lisp.h`
            'header_size': 8,
            'bool_header_size': 8,
            'word_size': 8,
            'CHAR_TABLE_STANDARD_SLOTS': '4 + (1 << CHARTAB_SIZE_BITS_0)',
            'LOG2_FLT_RADIX': 2,
            # MANY used in the DEFUN macro.
            # Actually, the macro concatenates MANY into DEFUN_MANY
            # instead of using MANY as a constant.
            # But for convenience here, we use MANY as a constant.
            # When the arg_max is MANY (-1), the function takes any number of arguments.
            'MANY': -1,
            'NULL': 0,
        },
        ignored_functions={
            # init_eval_once_for_pdumper: pure region init?
            'init_alloc_once_for_pdumper',
            # init_callproc initializes a lot of variables from runtime env.
            'init_callproc',
            # init_casetab_once: we don't want to explode the for loop...
            'init_casetab_once',
            # init_coding_once: coding categories, iso-2022, mule, etc.
            'init_coding_once',
            # init_editfns: user login names, etc.
            'init_editfns',
            # init_eval_once only initializes a local Lisp_Object (Vrun_hooks),
            # and call init_eval_once_for_pdumper to init specpdl.
            'init_eval_once',
            # init_eval: also specpdl initialization, and also Vquit_flag.
            'init_eval',
            # init_fileio: POSIX umask?
            'init_fileio',
            # init_keyboard: tons of event/signal bindings
            'init_keyboard',
            # init_syntax_once: tons of for loops
            'init_syntax_once',
            # init_timefns: set timezone, etc.
            'init_timefns',
            # init_window_once_for_pdumper: make initial frame
            'init_window_once_for_pdumper',
            # init_xdisp: implementation-specific initialization
            'init_xdisp',
            # init_xfaces: face_attr_sym?
            'init_xfaces',

            # syms_of_search_for_pdumper: implementation-specific allocation
            'syms_of_search_for_pdumper',
            # syms_of_timefns_for_pdumper: gmp library
            'syms_of_timefns_for_pdumper',
        },
        function_specific_configs=file_specific_configs,
    )
)
