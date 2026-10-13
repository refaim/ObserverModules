#include "../../archive.h"
#include "../support/archive_lazy_fixtures.h"

#include <catch2/catch_test_macros.hpp>
#include <catch2/matchers/catch_matchers_string.hpp>

#include <array>
#include <cstddef>
#include <cstdint>
#include <cwchar>
#include <filesystem>
#include <format>
#include <fstream>
#include <functional>
#include <iterator>
#include <memory>
#include <span>
#include <stdexcept>
#include <string>
#include <system_error>
#include <thread>
#include <vector>

#include <windows.h>

namespace
{
    using byte_buffer = std::vector<std::uint8_t>;

    constexpr std::streamoff body_offset = 16;
    constexpr std::size_t body_size = 16;

    // Layout: "FAKE" signature, a three-byte "HDR" header, padding to offset 16, then a
    // sixteen-byte body (0x40 .. 0x4F).
    byte_buffer make_archive_contents()
    {
        byte_buffer contents{'F', 'A', 'K', 'E', 'H', 'D', 'R'};
        while (contents.size() < static_cast<std::size_t>(body_offset)) {
            contents.push_back(0);
        }
        for (std::uint8_t index = 0; index < body_size; ++index) {
            contents.push_back(static_cast<std::uint8_t>(0x40 + index));
        }
        return contents;
    }

    // Same layout as make_archive_contents but with a body large enough that a raw copy
    // write cannot fit in a named-pipe buffer.
    byte_buffer make_large_archive_contents(const std::size_t large_body_size)
    {
        byte_buffer contents{'F', 'A', 'K', 'E', 'H', 'D', 'R'};
        while (contents.size() < static_cast<std::size_t>(body_offset)) {
            contents.push_back(0);
        }
        for (std::size_t index = 0; index < large_body_size; ++index) {
            contents.push_back(static_cast<std::uint8_t>(index & 0xFFU));
        }
        return contents;
    }

    std::span<const std::byte> leading_bytes(const byte_buffer &contents, const std::size_t count)
    {
        return std::as_bytes(std::span(contents)).first(count);
    }

    std::string read_file(const std::filesystem::path &path)
    {
        std::ifstream input(path, std::ios::binary);
        REQUIRE(input.is_open());
        return std::string(std::istreambuf_iterator<char>(input), std::istreambuf_iterator<char>());
    }

    void remove_best_effort(const std::filesystem::path &path) noexcept
    {
        std::error_code error;
        std::filesystem::remove(path, error);
    }

    test::support::fake_file_definition make_raw_definition(std::string header)
    {
        test::support::fake_file_definition definition;
        definition.path = "entry.bin";
        definition.header = std::move(header);
        definition.offset = body_offset;
        definition.compressed_body_size_in_bytes = static_cast<std::int64_t>(body_size);
        definition.uncompressed_body_size_in_bytes = static_cast<std::int64_t>(body_size);
        definition.magic = 0;
        return definition;
    }
} // namespace

TEST_CASE("archive lazy: get_archive_info receives the opened stream")
{
    const auto contents = make_archive_contents();
    const test::support::temporary_file file("open-stream", contents);
    test::support::fake_extractor_state state;
    state.files.push_back(make_raw_definition("HDR"));
    const test::support::fake_extractor_scope scope(state);

    archive::archive subject(std::make_unique<extractor::extractor>());
    const auto info = subject.open(file.path(), leading_bytes(contents, 4));

    REQUIRE(info.format == L"FakeFormat");
    REQUIRE(info.compression == L"-");
    REQUIRE(info.comment == L"fake comment");
    REQUIRE(state.archive_info_calls == 1);
    REQUIRE(state.archive_info_data_size == 4);
    REQUIRE(state.archive_info_stream_size == static_cast<std::streamoff>(contents.size()));
    REQUIRE(state.list_files_calls == 0);
}

TEST_CASE("archive lazy: a custom handler runs only during extraction")
{
    const auto contents = make_archive_contents();
    const test::support::temporary_file file("handler-scope", contents);

    int handler_calls = 0;
    test::support::fake_extractor_state state;
    auto definition = make_raw_definition("HDR");
    definition.extract = [&handler_calls](std::istream &, std::ostream &output,
                                          const std::function<void(std::int64_t)> &) {
        ++handler_calls;
        const std::string payload = "custom";
        output.write(payload.data(), std::ssize(payload));
    };
    state.files.push_back(std::move(definition));
    const test::support::fake_extractor_scope scope(state);

    archive::archive subject(std::make_unique<extractor::extractor>());
    static_cast<void>(subject.open(file.path(), {}));
    subject.prepare_files();
    static_cast<void>(subject.get_file(0));
    REQUIRE(handler_calls == 0);

    const auto destination = test::support::unused_path("handler-scope-out");
    subject.extract_file(0, destination, [](std::int64_t) {});
    REQUIRE(handler_calls == 1);
    REQUIRE(read_file(destination) == "custom");
    remove_best_effort(destination);
}

TEST_CASE("archive lazy: a custom handler may emit more bytes than the declared packed size")
{
    const auto contents = make_archive_contents();
    const test::support::temporary_file file("handler-longer", contents);

    test::support::fake_extractor_state state;
    auto definition = make_raw_definition("HDR");
    definition.extract = [](std::istream &, std::ostream &output, const std::function<void(std::int64_t)> &) {
        const std::string payload(4096, 'z');
        output.write(payload.data(), std::ssize(payload));
    };
    state.files.push_back(std::move(definition));
    const test::support::fake_extractor_scope scope(state);

    archive::archive subject(std::make_unique<extractor::extractor>());
    static_cast<void>(subject.open(file.path(), {}));
    subject.prepare_files();

    const auto destination = test::support::unused_path("handler-longer-out");
    subject.extract_file(0, destination, [](std::int64_t) {});
    const auto extracted = read_file(destination);
    REQUIRE(extracted.size() == 4096);
    REQUIRE(extracted == std::string(4096, 'z'));
    remove_best_effort(destination);
}

TEST_CASE("archive lazy: a custom handler is repeatable")
{
    const auto contents = make_archive_contents();
    const test::support::temporary_file file("handler-repeat", contents);

    int handler_calls = 0;
    test::support::fake_extractor_state state;
    auto definition = make_raw_definition("HDR");
    definition.extract = [&handler_calls](std::istream &, std::ostream &output,
                                          const std::function<void(std::int64_t)> &) {
        ++handler_calls;
        const std::string payload = "again";
        output.write(payload.data(), std::ssize(payload));
    };
    state.files.push_back(std::move(definition));
    const test::support::fake_extractor_scope scope(state);

    archive::archive subject(std::make_unique<extractor::extractor>());
    static_cast<void>(subject.open(file.path(), {}));
    subject.prepare_files();

    const auto first = test::support::unused_path("handler-repeat-a");
    const auto second = test::support::unused_path("handler-repeat-b");
    subject.extract_file(0, first, [](std::int64_t) {});
    subject.extract_file(0, second, [](std::int64_t) {});
    REQUIRE(handler_calls == 2);
    REQUIRE(read_file(first) == "again");
    REQUIRE(read_file(second) == "again");
    remove_best_effort(first);
    remove_best_effort(second);
}

TEST_CASE("archive lazy: a custom handler reads the source and reports progress")
{
    const auto contents = make_archive_contents();
    const test::support::temporary_file file("handler-source", contents);

    test::support::fake_extractor_state state;
    auto definition = make_raw_definition("HDR");
    definition.extract = [](std::istream &source, std::ostream &output,
                            const std::function<void(std::int64_t)> &report) {
        source.seekg(body_offset);
        std::array<char, body_size> buffer{};
        source.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        output.write(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        report(8);
        report(8);
    };
    state.files.push_back(std::move(definition));
    const test::support::fake_extractor_scope scope(state);

    archive::archive subject(std::make_unique<extractor::extractor>());
    static_cast<void>(subject.open(file.path(), {}));
    subject.prepare_files();

    std::vector<std::int64_t> progress;
    const auto destination = test::support::unused_path("handler-source-out");
    subject.extract_file(0, destination, [&progress](const std::int64_t value) { progress.push_back(value); });

    const std::string expected(reinterpret_cast<const char *>(contents.data() + body_offset), body_size);
    REQUIRE(read_file(destination) == expected);
    REQUIRE(progress == std::vector<std::int64_t>{8, 8});
    remove_best_effort(destination);
}

TEST_CASE("archive lazy: cancellation removes the partial destination and propagates")
{
    const auto contents = make_archive_contents();
    const test::support::temporary_file file("handler-cancel", contents);

    test::support::fake_extractor_state state;
    auto definition = make_raw_definition("HDR");
    definition.extract = [](std::istream &, std::ostream &output, const std::function<void(std::int64_t)> &report) {
        const std::string payload = "partial";
        output.write(payload.data(), std::ssize(payload));
        report(1);
    };
    state.files.push_back(std::move(definition));
    const test::support::fake_extractor_scope scope(state);

    archive::archive subject(std::make_unique<extractor::extractor>());
    static_cast<void>(subject.open(file.path(), {}));
    subject.prepare_files();

    const auto destination = test::support::unused_path("handler-cancel-out");
    REQUIRE_THROWS_AS(subject.extract_file(0, destination, [](std::int64_t) { throw archive::user_interrupt(); }),
                      archive::user_interrupt);
    REQUIRE(!(std::filesystem::exists(destination)));
}

TEST_CASE("archive lazy: a real source read failure is translated by the handler and mapped to read_error")
{
    const auto contents = make_archive_contents();
    const test::support::temporary_file file("handler-read", contents);

    test::support::fake_extractor_state state;
    auto definition = make_raw_definition("HDR");
    definition.extract = [](std::istream &source, std::ostream &output, const std::function<void(std::int64_t)> &) {
        const std::string payload = "partial";
        output.write(payload.data(), std::ssize(payload));

        // The archive enables failbit and badbit exceptions on the source stream, so
        // reading past the end is a genuine stream failure that surfaces as
        // std::ios_base::failure. The handler contract requires translating it to
        // extractor::read_error rather than letting it escape as a write error.
        std::array<char, body_size + 64> buffer{};
        source.seekg(body_offset);
        try {
            source.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
        } catch (const std::ios_base::failure &) {
            throw extractor::read_error();
        }
    };
    state.files.push_back(std::move(definition));
    const test::support::fake_extractor_scope scope(state);

    archive::archive subject(std::make_unique<extractor::extractor>());
    static_cast<void>(subject.open(file.path(), {}));
    subject.prepare_files();

    const auto destination = test::support::unused_path("handler-read-out");
    REQUIRE_THROWS_AS(subject.extract_file(0, destination, [](std::int64_t) {}), archive::read_error);
    REQUIRE(!(std::filesystem::exists(destination)));
}

TEST_CASE("archive lazy: a custom output io failure is cleaned up and mapped to write_error")
{
    const auto contents = make_archive_contents();
    const test::support::temporary_file file("handler-write", contents);

    test::support::fake_extractor_state state;
    auto definition = make_raw_definition("HDR");
    definition.extract = [](std::istream &, std::ostream &output, const std::function<void(std::int64_t)> &) {
        const std::string payload = "partial";
        output.write(payload.data(), std::ssize(payload));
        throw std::ios_base::failure("sink");
    };
    state.files.push_back(std::move(definition));
    const test::support::fake_extractor_scope scope(state);

    archive::archive subject(std::make_unique<extractor::extractor>());
    static_cast<void>(subject.open(file.path(), {}));
    subject.prepare_files();

    const auto destination = test::support::unused_path("handler-write-out");
    REQUIRE_THROWS_AS(subject.extract_file(0, destination, [](std::int64_t) {}), archive::write_error);
    REQUIRE(!(std::filesystem::exists(destination)));
}

TEST_CASE("archive lazy: an unrelated failure is preserved after cleanup")
{
    const auto contents = make_archive_contents();
    const test::support::temporary_file file("handler-other", contents);

    test::support::fake_extractor_state state;
    auto definition = make_raw_definition("HDR");
    definition.extract = [](std::istream &, std::ostream &output, const std::function<void(std::int64_t)> &) {
        const std::string payload = "partial";
        output.write(payload.data(), std::ssize(payload));
        throw std::runtime_error("boom");
    };
    state.files.push_back(std::move(definition));
    const test::support::fake_extractor_scope scope(state);

    archive::archive subject(std::make_unique<extractor::extractor>());
    static_cast<void>(subject.open(file.path(), {}));
    subject.prepare_files();

    const auto destination = test::support::unused_path("handler-other-out");
    REQUIRE_THROWS_WITH(subject.extract_file(0, destination, [](std::int64_t) {}), "boom");
    REQUIRE(!(std::filesystem::exists(destination)));
}

TEST_CASE("archive lazy: a failed output open preserves the existing path")
{
    const auto contents = make_archive_contents();
    const test::support::temporary_file file("handler-open-fail", contents);

    int handler_calls = 0;
    test::support::fake_extractor_state state;
    auto definition = make_raw_definition("HDR");
    definition.extract = [&handler_calls](std::istream &, std::ostream &, const std::function<void(std::int64_t)> &) {
        ++handler_calls;
    };
    state.files.push_back(std::move(definition));
    const test::support::fake_extractor_scope scope(state);

    archive::archive subject(std::make_unique<extractor::extractor>());
    static_cast<void>(subject.open(file.path(), {}));
    subject.prepare_files();

    const test::support::temporary_directory destination("handler-open-fail-out");
    REQUIRE_THROWS_AS(subject.extract_file(0, destination.path(), [](std::int64_t) {}), archive::write_error);
    REQUIRE(std::filesystem::is_directory(destination.path()));
    REQUIRE(handler_calls == 0);
}

TEST_CASE("archive lazy: an empty handler keeps the raw header and body copy")
{
    const auto contents = make_archive_contents();
    const test::support::temporary_file file("raw-with-header", contents);

    test::support::fake_extractor_state state;
    state.files.push_back(make_raw_definition("HDR"));
    const test::support::fake_extractor_scope scope(state);

    archive::archive subject(std::make_unique<extractor::extractor>());
    static_cast<void>(subject.open(file.path(), {}));
    subject.prepare_files();

    std::vector<std::int64_t> progress;
    const auto destination = test::support::unused_path("raw-with-header-out");
    subject.extract_file(0, destination, [&progress](const std::int64_t value) { progress.push_back(value); });

    const std::string expected =
        "HDR" + std::string(reinterpret_cast<const char *>(contents.data() + body_offset), body_size);
    REQUIRE(read_file(destination) == expected);
    REQUIRE(progress == std::vector<std::int64_t>{static_cast<std::int64_t>(body_size)});
    remove_best_effort(destination);
}

TEST_CASE("archive lazy: an empty handler without a header copies only the body")
{
    const auto contents = make_archive_contents();
    const test::support::temporary_file file("raw-no-header", contents);

    test::support::fake_extractor_state state;
    state.files.push_back(make_raw_definition(""));
    const test::support::fake_extractor_scope scope(state);

    archive::archive subject(std::make_unique<extractor::extractor>());
    static_cast<void>(subject.open(file.path(), {}));
    subject.prepare_files();

    const auto destination = test::support::unused_path("raw-no-header-out");
    subject.extract_file(0, destination, [](std::int64_t) {});

    const std::string expected(reinterpret_cast<const char *>(contents.data() + body_offset), body_size);
    REQUIRE(read_file(destination) == expected);
    remove_best_effort(destination);
}

TEST_CASE("archive lazy: the raw path maps a failing seek to read_error")
{
    const auto contents = make_archive_contents();
    const test::support::temporary_file file("raw-seek-error", contents);

    test::support::fake_extractor_state state;
    auto negative = make_raw_definition("");
    negative.offset = -1;
    state.files.push_back(std::move(negative));
    const test::support::fake_extractor_scope scope(state);

    archive::archive subject(std::make_unique<extractor::extractor>());
    static_cast<void>(subject.open(file.path(), {}));
    subject.prepare_files();

    const auto destination = test::support::unused_path("raw-seek-error-out");
    REQUIRE_THROWS_AS(subject.extract_file(0, destination, [](std::int64_t) {}), archive::read_error);
    remove_best_effort(destination);
}

TEST_CASE("archive lazy: the raw path maps a failing read to read_error")
{
    const auto contents = make_archive_contents();
    const test::support::temporary_file file("raw-read-error", contents);

    test::support::fake_extractor_state state;
    auto truncated = make_raw_definition("");
    truncated.compressed_body_size_in_bytes = 4096;
    state.files.push_back(std::move(truncated));
    const test::support::fake_extractor_scope scope(state);

    archive::archive subject(std::make_unique<extractor::extractor>());
    static_cast<void>(subject.open(file.path(), {}));
    subject.prepare_files();

    const auto destination = test::support::unused_path("raw-read-error-out");
    REQUIRE_THROWS_AS(subject.extract_file(0, destination, [](std::int64_t) {}), archive::read_error);
    remove_best_effort(destination);
}

TEST_CASE("archive lazy: the raw path maps an output io failure to write_error")
{
    // The body is larger than the pipe buffer, so the raw copy's first output write must
    // block until the server disconnects: the disconnect is ordered strictly before the
    // write failure without any timing sleep, removing the previous close/flush race.
    constexpr std::size_t large_body_size = std::size_t{512} * 1024;
    const auto contents = make_large_archive_contents(large_body_size);
    const test::support::temporary_file file("raw-write-error", contents);

    test::support::fake_extractor_state state;
    auto definition = make_raw_definition("HDR");
    definition.compressed_body_size_in_bytes = static_cast<std::int64_t>(large_body_size);
    definition.uncompressed_body_size_in_bytes = static_cast<std::int64_t>(large_body_size);
    state.files.push_back(std::move(definition));
    const test::support::fake_extractor_scope scope(state);

    archive::archive subject(std::make_unique<extractor::extractor>());
    static_cast<void>(subject.open(file.path(), {}));
    subject.prepare_files();

    const auto pipe_name = std::format(L"\\\\.\\pipe\\unpackrat-lazy-write-{}", GetCurrentProcessId());
    const auto pipe =
        CreateNamedPipeW(pipe_name.c_str(), PIPE_ACCESS_INBOUND | FILE_FLAG_FIRST_PIPE_INSTANCE,
                         PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT, 1, 64 * 1024, 64 * 1024, 0, nullptr);
    REQUIRE(pipe != INVALID_HANDLE_VALUE);
    std::jthread server([pipe] {
        static_cast<void>(ConnectNamedPipe(pipe, nullptr));
        static_cast<void>(CloseHandle(pipe));
    });

    REQUIRE_THROWS_AS(subject.extract_file(0, pipe_name, [](std::int64_t) {}), archive::write_error);
}

TEST_CASE("archive lazy: repeated prepare and out-of-range lookup are rejected")
{
    const auto contents = make_archive_contents();
    const test::support::temporary_file file("prepare-index", contents);

    test::support::fake_extractor_state state;
    state.files.push_back(make_raw_definition("HDR"));
    const test::support::fake_extractor_scope scope(state);

    archive::archive subject(std::make_unique<extractor::extractor>());
    static_cast<void>(subject.open(file.path(), {}));
    subject.prepare_files();
    subject.prepare_files();
    REQUIRE(state.list_files_calls == 1);
    REQUIRE_THROWS_AS(subject.get_file(1), std::out_of_range);
}

TEST_CASE("archive lazy: a listing read failure maps to read_error")
{
    const auto contents = make_archive_contents();
    const test::support::temporary_file file("prepare-read-error", contents);

    test::support::fake_extractor_state state;
    state.list_files_read_error = true;
    const test::support::fake_extractor_scope scope(state);

    archive::archive subject(std::make_unique<extractor::extractor>());
    static_cast<void>(subject.open(file.path(), {}));
    REQUIRE_THROWS_AS(subject.prepare_files(), archive::read_error);
}

TEST_CASE("archive lazy: open rejects a mismatched prefix and unreadable path")
{
    const auto contents = make_archive_contents();
    const test::support::temporary_file file("open-reject", contents);

    test::support::fake_extractor_state state;
    state.files.push_back(make_raw_definition("HDR"));
    const test::support::fake_extractor_scope scope(state);

    archive::archive subject(std::make_unique<extractor::extractor>());

    const byte_buffer mismatch{0, 0, 0, 0};
    REQUIRE_THROWS_WITH(subject.open(file.path(), std::as_bytes(std::span(mismatch))), "Signature mismatch");

    const byte_buffer short_prefix{'F', 'A'};
    REQUIRE_THROWS_WITH(subject.open(file.path(), std::as_bytes(std::span(short_prefix))), "Signature mismatch");

    const auto missing = test::support::unused_path("open-missing");
    REQUIRE_THROWS_WITH(subject.open(missing, {}), "Failed to open file");
}

TEST_CASE("archive lazy: an empty signature accepts any prefix")
{
    const auto contents = make_archive_contents();
    const test::support::temporary_file file("open-empty-signature", contents);

    test::support::fake_extractor_state state;
    state.signature.clear();
    state.files.push_back(make_raw_definition("HDR"));
    const test::support::fake_extractor_scope scope(state);

    archive::archive subject(std::make_unique<extractor::extractor>());
    const byte_buffer arbitrary{9, 9, 9, 9};
    const auto info = subject.open(file.path(), std::as_bytes(std::span(arbitrary)));
    REQUIRE(info.format == L"FakeFormat");
    REQUIRE(state.archive_info_data_size == 4);
}

TEST_CASE("archive lazy: unused_path is unique per call and carries the process id")
{
    const auto first = test::support::unused_path("unique-probe");
    const auto second = test::support::unused_path("unique-probe");

    REQUIRE(first != second);
    REQUIRE(!(std::filesystem::exists(first)));
    REQUIRE(first.filename().string().find(std::to_string(GetCurrentProcessId())) != std::string::npos);
}

TEST_CASE("archive lazy: archive context forwards the source path and password without global state")
{
    const auto contents = make_archive_contents();
    const test::support::temporary_file file("open-context", contents);
    test::support::fake_extractor_state state;
    const test::support::fake_extractor_scope scope(state);
    archive::archive subject(std::make_unique<extractor::extractor>());
    static_cast<void>(subject.open(file.path(), leading_bytes(contents, 4), "0123456789abcdef"));
    CHECK(state.archive_info_path == file.path());
    CHECK(state.archive_info_password == "0123456789abcdef");
}

TEST_CASE("archive lazy: reopening destroys old callbacks before replacing their borrowed storage")
{
    struct lifetime_state final : extractor::storage_state
    {
        bool &alive;
        explicit lifetime_state(bool &flag) : alive(flag)
        {
            alive = true;
        }
        ~lifetime_state() override
        {
            alive = false;
        }
    };
    struct watched_capture final
    {
        bool &watch;
        bool &alive;
        bool &released_before_owner;
        ~watched_capture()
        {
            if (watch) {
                released_before_owner = alive;
            }
        }
    };
    bool watch = false;
    bool old_alive = false;
    bool new_alive = false;
    bool released_before_owner = false;
    unsigned generation = 0;
    const auto contents = make_archive_contents();
    const test::support::temporary_file first("reopen-first", contents);
    const test::support::temporary_file second("reopen-second", contents);
    test::support::fake_extractor_state state;
    state.storage_factory = [&] { return std::make_unique<lifetime_state>(generation++ == 0 ? old_alive : new_alive); };
    auto old_entry = make_raw_definition("HDR");
    old_entry.path = "first.bin";
    old_entry.extract = [capture = watched_capture{watch, old_alive, released_before_owner}](
                            std::istream &, std::ostream &, const std::function<void(std::int64_t)> &) {
        static_cast<void>(capture);
    };
    state.files.push_back(std::move(old_entry));
    const test::support::fake_extractor_scope scope(state);
    archive::archive subject(std::make_unique<extractor::extractor>());
    static_cast<void>(subject.open(first.path(), {}));
    subject.prepare_files();
    REQUIRE(subject.get_file(0).path == "first.bin");
    REQUIRE(old_alive);
    state.files.clear();
    auto replacement = make_raw_definition("HDR");
    replacement.path = "second.bin";
    state.files.push_back(std::move(replacement));
    watch = true;
    REQUIRE(watch);
    static_cast<void>(subject.open(second.path(), {}));
    CHECK(released_before_owner);
    CHECK(!(old_alive));
    CHECK(new_alive);
    subject.prepare_files();
    CHECK(subject.get_file(0).path == "second.bin");
}

TEST_CASE("archive lazy: preparation before any successful open fails without calling the extractor")
{
    test::support::fake_extractor_state state;
    const test::support::fake_extractor_scope scope(state);
    archive::archive subject(std::make_unique<extractor::extractor>());
    CHECK_THROWS_AS(subject.prepare_files(), archive::read_error);
    CHECK(state.list_files_calls == 0);
}

TEST_CASE("archive lazy: failed archive information invalidates preparation after a prior success")
{
    const auto contents = make_archive_contents();
    const test::support::temporary_file first("open-success", contents);
    const test::support::temporary_file second("open-info-failure", contents);
    test::support::fake_extractor_state state;
    state.files.push_back(make_raw_definition("HDR"));
    const test::support::fake_extractor_scope scope(state);
    archive::archive subject(std::make_unique<extractor::extractor>());
    static_cast<void>(subject.open(first.path(), {}));
    subject.prepare_files();
    REQUIRE(state.list_files_calls == 1);
    state.storage_factory = []() -> std::unique_ptr<extractor::storage_state> {
        throw std::runtime_error("fixture info failure");
    };
    CHECK_THROWS_AS(subject.open(second.path(), {}), std::runtime_error);
    CHECK_THROWS_AS(subject.get_file(0), std::out_of_range);
    CHECK_THROWS_AS(subject.prepare_files(), archive::read_error);
    CHECK(state.list_files_calls == 1);
}

TEST_CASE("sdk wchar: comparison accepts valid two-byte aligned wchar_t arrays", "[sdk][ubsan][wmemcmp]")
{
    struct alignas(8) wide_storage
    {
        wchar_t prefix = 0;
        std::array<wchar_t, 15> text{};
    };
    static_assert(alignof(wchar_t) == 2);
    static_assert(offsetof(wide_storage, text) == 2);
    static_assert(sizeof(wide_storage) == 32);
    wide_storage left;
    left.text = {L'a', L'b', L'c', L'd'};
    auto right = left;
    CHECK(std::wmemcmp(left.text.data(), right.text.data(), 4) == 0);
    right.text[3] = L'e';
    CHECK(std::wmemcmp(left.text.data(), right.text.data(), 4) < 0);
    CHECK(std::wmemcmp(right.text.data(), left.text.data(), 4) > 0);
}
