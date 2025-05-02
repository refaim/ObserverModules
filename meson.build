project(
    'zanzarah',
    'cpp',
    default_options : [
        'warning_level=3',
        'cpp_std=c++20',
    ]
)

cpp_args = [
    '-O2',
    # Export only explicitly marked symbols
    '-fvisibility=hidden',
]

link_args = [
    '-static',
]

if get_option('buildtype') == 'debug'
    cpp_args += [
        '-gcodeview',
        '-O1',
        # https://clang.llvm.org/docs/AddressSanitizer.html#id3
        #        '-fsanitize=address',
        #        '-fsanitize-address-use-after-return=always',
        #        '-fno-omit-frame-pointer',
        #        '-fno-optimize-sibling-calls',
        # https://clang.llvm.org/docs/MemorySanitizer.html#id3
        #        '-fsanitize=undefined',
    ]
    link_args += [
        '-Wl,/DEBUG',
        #        '-fsanitize=address',
        #        '-fsanitize=undefined',
    ]
endif

shared_library(
    'zanzarah',
    'dll.cpp', 'archive.cpp',
    name_prefix : '',
    name_suffix : 'so',
    cpp_args : cpp_args,
    link_args : link_args,
)