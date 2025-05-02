project(
    'zanzarah',
    'cpp',
    default_options : [
        'cpp_std=c++20',
        'warning_level=3',
        'b_vscrt=static_from_buildtype',
    ]
)

shared_library(
    'zanzarah',
    'dll.cpp', 'archive.cpp',
    name_prefix : '',
    name_suffix : 'so',
    cpp_args : ['/analyze'],
    vs_module_defs : 'dll.def',
)