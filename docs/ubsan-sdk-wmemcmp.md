# UBSan: Windows SDK wmemcmp alignment

Decision: accept one external-code exception for alignment instrumentation inside the
unmodified Windows SDK `wmemcmp` function, in the `UBSan` configuration only.
The ignorelist contains exactly `[alignment]` and `fun:wmemcmp`. No project function,
source file, or other sanitizer check is excluded. The build keeps
`-fsanitize=undefined` and `-fno-sanitize-recover=all`.

## Observed defect

The affected combination is Windows SDK 10.0.26100.0 UCRT and Visual Studio's bundled
LLVM 19.1.5 on x64. In `ucrt/wchar.h:344-347`, `wmemcmp` accepts `wchar_t const*`.
Windows `wchar_t` requires 2-byte alignment. Its four-character x64 tail nevertheless
casts these pointers to `unsigned __int64*` and dereferences them at lines 458-459,
which requires 8-byte alignment in C++. Correct inputs can therefore trigger UBSan
inside the SDK. The x64 machine supports the unaligned loads; the exception does not
change the SDK source or claim that the casts satisfy portable C++ alignment rules.

The first full-suite report arose from an ordinary `std::wstring` equality check.
Changing one assertion to a character loop would avoid that call without repairing
other standard-library consumers of the same SDK routine. Replacing standard
wide-string operations throughout the project would add unrelated first-party
infrastructure. The exact vendor function exception preserves those ordinary API
contracts while leaving project alignment instrumentation enabled.

## Scope and evidence

`build/tests/test_ubsan_vendor_policy.py` checks the exact UBSan-only compiler options,
the preserved fatal undefined-behavior flags, and the two exact ignorelist entries.
A wildcard, file rule, extra function/category, or another configuration fails that
contract. These tests first failed because the flag and explicit list were absent.

The native test tagged `[sdk][ubsan][wmemcmp]` in `src/tests/unit/archive_lazy.cpp`
places a `wchar_t` array at offset 2 within an 8-byte-aligned 32-byte object. Static
assertions establish both its valid wchar alignment and exact layout. It checks
equal, less-than and greater-than comparisons in every native test configuration.

Four independent clang-cl UBSan processes established the exception's limits:

* Valid wchar inputs without the list: exit 1, alignment error in SDK `wchar.h:458`.
* The same valid inputs with the exact list: exit 0, correct equality result.
* With the same list, a deliberate misaligned uint64 load in a separate first-party
  function: exit 1, alignment error at that function.
* With the same list, a deliberate null input to `wmemcmp`: exit 1, null-pointer
  error inside `wmemcmp`. Other checks remain enabled even in the exempted function.

The local evidence is retained under
`out/godot-samples/wmemcmp-ubsan-investigation`, with sources, build/run logs and
`results.json`; the initial full-suite failure is in
`out/godot-samples/encryption-ubsan-attempt.log`. These are diagnostic artifacts, not
required test inputs. The committed native regression remains self-contained.

## Revisit rule

Re-run the valid-input regression without the ignorelist whenever Windows SDK or
LLVM changes. Remove the exception when the vendor implementation no longer reports
this alignment defect. Any further sanitizer finding needs its own diagnosis and
review; this decision does not authorize expanding the list. The exemption can
hide a future alignment defect inside `wmemcmp`, which is why its exact scope,
known SDK version and removal condition are recorded here.

Clang documents sanitizer-specific sections and exact function rules in its
[special-case-list reference](https://clang.llvm.org/docs/SanitizerSpecialCaseList.html)
and [UBSan ignorelist reference](https://clang.llvm.org/docs/UndefinedBehaviorSanitizer.html#suppressing-errors-in-recompiled-code-ignorelist).
