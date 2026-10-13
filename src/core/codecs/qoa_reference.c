/*
 * Vendored, unmodified QOA reference implementation, compiled once as C in its own translation
 * unit. qoa_codec.cpp includes only qoa.h's declarations and drives the decoder through the
 * caller-owned-buffer entry points, so no third-party type or symbol reaches our public API.
 *
 *   godot @ 876b290332ec6f2e6d173d08162a02aa7e6ca46d, thirdparty/misc/qoa.h
 *   sha256 ab501e91f4afd18c811e689713e5fd3956030562bf80cda784d304af17133f01
 *   MIT (c) 2023 Dominic Szablewski (licenses/qoa.txt); see third_party/qoa/PROVENANCE.json.
 *
 * The reference is C, not C++: the allocating helpers assign a void* to a typed pointer, which a
 * conforming C++ compiler rejects. Compiling it as C keeps the header byte-for-byte unchanged and
 * lets this project's per-source instrumentation policy leave vendor code uninstrumented.
 */
#define QOA_NO_STDIO
#define QOA_IMPLEMENTATION
#pragma warning(push, 0)
/* The encoder path leaves ``best_lms`` assigned only inside a conditional slice search, so MSVC's
 * C flow analysis reports a false-positive C4701 that the warning-level push above does not
 * silence. Disable exactly that one diagnostic for this single vendored implementation include
 * and restore it with the matching pop; no global or project-wide suppression is introduced. */
#pragma warning(disable : 4701)
#include <../third_party/qoa/qoa.h>
#pragma warning(pop)
