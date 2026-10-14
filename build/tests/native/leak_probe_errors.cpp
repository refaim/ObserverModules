// Focused regression fixtures for the leak-probe process entry point.
//
// The probe reports its two fatal outcomes -- a std::exception and an unknown
// failure -- from catch handlers inside main().  Those reports must never
// escape main(): a stdio report cannot throw, but a C++ iostream report does,
// so a throwing std::cerr streambuf turns the report itself into an uncaught
// exception.  These fixtures call the real entry point with a throwing
// std::cerr (and, for the unknown-failure path, a throwing std::cout) installed
// and require the entry point to still return EXIT_FAILURE without letting the
// report exception escape.
//
// A Streams-only Catch2 build cannot capture the probe's C stdio at the
// file-descriptor level, so the exact report bytes are verified across a
// process boundary by the companion script ``check_leak_probe_errors.py``.
// These fixtures therefore assert only the in-process invariants an ordinary
// Catch2 build can judge: the exact fatal exit code and the guarantee that no
// report exception escapes the entry point.
//
// `main` is renamed only for the duration of including the probe translation
// unit so the real entry point can be invoked directly; the probe source, its
// Release-only guard and its public ABI stay untouched.  The suite's own
// ``src/tests/main.cpp`` supplies the process main() through
// ``Catch::Session().run``; the build links only ``Catch2.lib`` -- there is no
// ``Catch2Main.lib`` -- so this translation unit must not rely on a
// Catch2-provided entry point.

#ifndef NOMINMAX
#define NOMINMAX
#endif

#define main unpackrat_leak_probe_test_main
#include "../../../src/tests/leaks/probe.cpp"
#undef main

#include <catch2/catch_test_macros.hpp>

#include <cstdlib>
#include <ios>
#include <iostream>
#include <ostream>
#include <stdexcept>
#include <streambuf>
#include <string_view>

#include <windows.h>

namespace
{
    // A private fault type, deliberately not derived from std::exception, so a
    // faulted stream drives the probe's catch(...) path rather than its
    // std::exception path.
    struct probe_fault final
    {
    };

    // Throws probe_fault from every write.  Installed over std::cerr (and
    // std::cout) to prove the probe never routes a fatal report through a
    // throwing C++ stream.
    class faulting_streambuf final : public std::streambuf
    {
      protected:
        std::streamsize xsputn(const char *, std::streamsize) override
        {
            throw probe_fault{};
        }

        int_type overflow(int_type) override
        {
            throw probe_fault{};
        }
    };

    // Borrows an ostream, swaps in a faulting buffer with badbit exceptions
    // enabled, and restores the borrowed state on destruction in a defined,
    // non-throwing order: drop the exception mask, put the original buffer back,
    // clear any error state, then re-arm the original mask.
    class faulting_stream final
    {
      public:
        explicit faulting_stream(std::ostream &stream)
            : stream_(stream), original_buffer_(stream.rdbuf()), original_mask_(stream.exceptions())
        {
            stream_.exceptions(std::ios_base::goodbit);
            stream_.rdbuf(&buffer_);
            stream_.exceptions(original_mask_ | std::ios_base::badbit);
        }

        ~faulting_stream()
        {
            stream_.exceptions(std::ios_base::goodbit);
            stream_.rdbuf(original_buffer_);
            stream_.clear();
            stream_.exceptions(original_mask_);
        }

        faulting_stream(const faulting_stream &) = delete;
        faulting_stream &operator=(const faulting_stream &) = delete;
        faulting_stream(faulting_stream &&) = delete;
        faulting_stream &operator=(faulting_stream &&) = delete;

        [[nodiscard]] std::streambuf *original_buffer() const noexcept
        {
            return original_buffer_;
        }

      private:
        std::ostream &stream_;
        std::streambuf *original_buffer_;
        std::ios_base::iostate original_mask_;
        faulting_streambuf buffer_;
    };
} // namespace

TEST_CASE("leak-probe fault injection throws a non-std exception and restores the stream")
{
    std::streambuf *const original_buffer = std::cerr.rdbuf();
    const std::ios_base::iostate original_mask = std::cerr.exceptions();

    bool string_write_threw = false;
    bool character_write_threw = false;
    {
        const faulting_stream fault(std::cerr);
        CHECK(fault.original_buffer() == original_buffer);
        CHECK((std::cerr.exceptions() & std::ios_base::badbit) != 0);

        try {
            std::cerr << "fault";
        } catch (const probe_fault &) {
            string_write_threw = true;
        }
        std::cerr.clear();

        try {
            std::cerr.put('x');
        } catch (const probe_fault &) {
            character_write_threw = true;
        }
        std::cerr.clear();
    }

    CHECK(string_write_threw);
    CHECK(character_write_threw);
    CHECK(std::cerr.rdbuf() == original_buffer);
    CHECK(std::cerr.exceptions() == original_mask);
}

TEST_CASE("leak-probe reports an invalid command line from the std::exception handler")
{
    char program[] = "leak-probe";
    char option[] = "--bogus";
    char *arguments[]{program, option, nullptr};

    int exit_code = EXIT_SUCCESS;
    bool report_exception_escaped = false;
    {
        const faulting_stream fault(std::cerr);
        try {
            exit_code = unpackrat_leak_probe_test_main(2, arguments);
        } catch (...) {
            report_exception_escaped = true;
        }
    }

    CHECK_FALSE(report_exception_escaped);
    CHECK(exit_code == EXIT_FAILURE);
}

TEST_CASE("leak-probe reports an unknown failure from the catch-all handler")
{
    // ``--help`` drives the same catch-all path as a real run: parse_options
    // prints usage to std::cout and only exits once that write succeeds, so a
    // throwing std::cout faults before the process would exit and before any
    // large measurement fixture is allocated.
    char program[] = "leak-probe";
    char option[] = "--help";
    char *arguments[]{program, option, nullptr};

    int exit_code = EXIT_SUCCESS;
    bool report_exception_escaped = false;
    {
        const faulting_stream error_fault(std::cerr);
        const faulting_stream output_fault(std::cout);
        try {
            exit_code = unpackrat_leak_probe_test_main(2, arguments);
        } catch (...) {
            report_exception_escaped = true;
        }
    }

    CHECK_FALSE(report_exception_escaped);
    CHECK(exit_code == EXIT_FAILURE);
}

// ``resolve_export`` wraps ``GetProcAddress`` and must either hand back a usable callable or
// raise its documented ``std::runtime_error``.  The probe's load path resolves only the module
// ABI entry points, so this focused regression pins the helper against a real Kernel32 export:
// the borrowed module handle is never released, the resolved pointer calls like the export it
// names, and an absent name still raises the documented error.
TEST_CASE("resolve_export resolves a real Kernel32 export and rejects a missing one")
{
    const HMODULE kernel32 = GetModuleHandleW(L"kernel32.dll");
    REQUIRE(kernel32 != nullptr);

    const auto current_process_id = resolve_export<DWORD(WINAPI *)()>(kernel32, "GetCurrentProcessId");
    REQUIRE(current_process_id != nullptr);
    CHECK(current_process_id() == ::GetCurrentProcessId());

    try {
        (void)resolve_export<DWORD(WINAPI *)()>(kernel32, "unpackrat_regression_absent_export");
        FAIL("resolve_export accepted a missing export");
    } catch (const std::runtime_error &error) {
        CHECK(std::string_view(error.what()) == "Required module export is absent: unpackrat_regression_absent_export");
    }
}
