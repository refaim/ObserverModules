#include "../../modules/godot/archive_session.h"
#include "../support/archive_lazy_fixtures.h"
#include "../support/godot_crypto_fixtures.h"
#include "../support/godot_key_fixtures.h"

#include <array>
#include <cstddef>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <span>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include <catch2/catch_test_macros.hpp>
#include <fcntl.h>
#include <io.h>
#include <share.h>
#include <windows.h>

namespace
{
    using test::support::godot_bytes;
    using unpackrat::godot::archive_session;
    using unpackrat::godot::session_limits;

    void write_file(const std::filesystem::path &path, const std::span<const std::uint8_t> bytes)
    {
        std::ofstream out(path, std::ios::binary);
        REQUIRE(out.is_open());
        out.write(reinterpret_cast<const char *>(bytes.data()), static_cast<std::streamsize>(bytes.size()));
        out.close();
        REQUIRE(out.good());
    }

    void write_pe(const std::filesystem::path &path, const test::support::key_fixture &fixture)
    {
        write_file(path, {reinterpret_cast<const std::uint8_t *>(fixture.image.data()), fixture.image.size()});
    }

    std::string hex_key(const unpackrat::codecs::key256 &key)
    {
        constexpr std::string_view digits = "0123456789abcdef";
        std::string text;
        for (const auto byte : key) {
            const auto value = std::to_integer<unsigned>(byte);
            text.push_back(digits[value >> 4]);
            text.push_back(digits[value & 15]);
        }
        return text;
    }

    class saved_file_acl final
    {
      public:
        explicit saved_file_acl(const std::filesystem::path &path) : path_(path)
        {
            DWORD needed = 0;
            static_cast<void>(GetFileSecurityW(path_.c_str(), DACL_SECURITY_INFORMATION, nullptr, 0, &needed));
            REQUIRE(needed > 0);
            bytes_.resize(needed);
            REQUIRE(GetFileSecurityW(path_.c_str(), DACL_SECURITY_INFORMATION, bytes_.data(), needed, &needed));
        }
        ~saved_file_acl()
        {
            static_cast<void>(SetFileSecurityW(path_.c_str(), DACL_SECURITY_INFORMATION, bytes_.data()));
        }
        saved_file_acl(const saved_file_acl &) = delete;
        saved_file_acl &operator=(const saved_file_acl &) = delete;

        void protect_from_inheritance()
        {
            auto copy = bytes_;
            REQUIRE(SetSecurityDescriptorControl(copy.data(), SE_DACL_PROTECTED, SE_DACL_PROTECTED));
            REQUIRE(SetFileSecurityW(path_.c_str(), DACL_SECURITY_INFORMATION, copy.data()));
        }
        void deny_directory_listing()
        {
            SECURITY_DESCRIPTOR descriptor{};
            ACL empty{};
            REQUIRE(InitializeSecurityDescriptor(&descriptor, SECURITY_DESCRIPTOR_REVISION));
            REQUIRE(InitializeAcl(&empty, sizeof(empty), ACL_REVISION));
            REQUIRE(SetSecurityDescriptorDacl(&descriptor, TRUE, &empty, FALSE));
            REQUIRE(SetFileSecurityW(path_.c_str(), DACL_SECURITY_INFORMATION, &descriptor));
        }

      private:
        std::filesystem::path path_;
        std::vector<std::byte> bytes_;
    };

    godot_bytes plain_pack()
    {
        test::support::godot_pck_options options;
        test::support::godot_entry_options entry;
        entry.path = "res://kept.bin";
        entry.payload = {'k'};
        options.entries.push_back(entry);
        return test::support::build_godot_pck(options);
    }
} // namespace

TEST_CASE("godot session: owns a validated standalone source and releases its read lease")
{
    const test::support::temporary_directory folder("godot-session-plain");
    const auto path = folder.path() / "game.pck";
    write_file(path, plain_pack());
    {
        archive_session session(path);
        CHECK(session.source_path() == path);
        REQUIRE(session.index().entries.size() == 1);
        CHECK(session.index().entries.front().path == "kept.bin");
        CHECK(session.key() == nullptr);
        std::error_code error;
        std::filesystem::resize_file(path, 0, error);
        CHECK(error);
        std::fstream writer(path, std::ios::in | std::ios::out | std::ios::binary);
        CHECK(!(writer.is_open()));
        auto &input = session.stream();
        input.clear();
        input.seekg(0);
        std::array<char, 4> magic{};
        input.read(magic.data(), 4);
        CHECK(std::string_view(magic.data(), magic.size()) == "GDPC");
    }
    std::error_code error;
    std::filesystem::resize_file(path, 0, error);
    CHECK(!(error));
}

TEST_CASE("godot session: EXE resolves an exact-stem sidecar only when no embedded pack exists")
{
    const test::support::temporary_directory folder("godot-session-sidecar");
    const auto executable = folder.path() / "game.EXE";
    const auto sidecar = folder.path() / "game.pck";
    write_file(sidecar, plain_pack());
    SECTION("ordinary runtime")
    {
        write_file(executable, test::support::minimal_pe_without_pack());
    }
    SECTION("standard empty pck placeholder")
    {
        test::support::godot_pe_section section;
        section.data.resize(512);
        auto bytes = test::support::build_godot_pe(section);
        test::support::patch_u32(bytes, 0x188 + 8, 8);
        write_file(executable, bytes);
    }
    SECTION("corrupt claimed embedded pack does not mask corruption")
    {
        test::support::godot_pe_section section;
        section.data = {'G', 'D', 'P', 'C', 255};
        write_file(executable, test::support::build_godot_pe(section));
        CHECK_THROWS_AS(archive_session(executable), std::runtime_error);
        return;
    }
    archive_session session(executable);
    CHECK(session.source_path() == sidecar);
    REQUIRE(session.index().entries.size() == 1);
    CHECK(session.index().entries.front().path == "kept.bin");
}

TEST_CASE("godot session: an embedded footer wins over a same-stem sidecar")
{
    const test::support::temporary_directory folder("godot-session-footer");
    const auto path = folder.path() / "game.exe";
    test::support::godot_pck_options options;
    test::support::godot_entry_options entry;
    entry.path = "res://embedded.bin";
    entry.payload = {'e'};
    options.entries.push_back(entry);
    write_file(path, test::support::wrap_godot_pck_in_footer(options, 128));
    write_file(folder.path() / "game.pck", plain_pack());
    archive_session session(path);
    CHECK(session.source_path() == path);
    CHECK(session.index().entries.front().path == "embedded.bin");
}

TEST_CASE("godot session: missing sources and runtimes without a sidecar fail explicitly")
{
    const test::support::temporary_directory folder("godot-session-invalid");
    const auto path = folder.path() / "game.exe";
    CHECK_THROWS_AS(archive_session(path), std::runtime_error);
    write_file(path, test::support::minimal_pe_without_pack());
    CHECK_THROWS_AS(archive_session(path), std::runtime_error);
    write_file(folder.path() / "other.pck", plain_pack());
    CHECK_THROWS_AS(archive_session(path), std::runtime_error);
    write_file(folder.path() / "game.pck", godot_bytes{'x'});
    CHECK_THROWS_AS(archive_session(path), std::runtime_error);
    write_file(path, godot_bytes{'x'});
    CHECK_THROWS_AS(archive_session(path), std::runtime_error);
}

TEST_CASE("godot session: explicit hexadecimal key is verified before the archive is prepared")
{
    const test::support::temporary_directory folder("godot-session-password");
    const auto path = folder.path() / "encrypted.pck";
    for (const bool directory : {false, true}) {
        CAPTURE(directory);
        write_file(path, test::support::encrypted_pck(3, directory, true));
        SECTION("correct key")
        {
            archive_session session(path, hex_key(test::support::godot_test_key()));
            REQUIRE(session.key() != nullptr);
            CHECK(*session.key() == test::support::godot_test_key());
            REQUIRE(session.index().entries.size() == 1);
            CHECK(session.index().entries.front().logical_size == 5);
        }
        SECTION("uppercase key")
        {
            auto text = hex_key(test::support::godot_test_key());
            for (auto &ch : text) {
                if (ch >= 'a' && ch <= 'f') {
                    ch = static_cast<char>(ch - 'a' + 'A');
                }
            }
            archive_session session(path, text);
            CHECK(session.key() != nullptr);
        }
        SECTION("missing malformed and wrong keys request the native password prompt")
        {
            for (const auto &text : {std::string{}, std::string(63, '0'), std::string(64, 'g'), std::string(64, '#'),
                                     std::string(62, '0') + "0g", std::string(65, '0'), std::string(64, '0')}) {
                CAPTURE(text.size());
                CHECK_THROWS_AS(archive_session(path, text), extractor::password_required);
            }
        }
        SECTION("validation work budget is enforced")
        {
            session_limits limits;
            limits.max_validation_bytes = 0;
            CHECK_THROWS_AS(archive_session(path, hex_key(test::support::godot_test_key()), limits),
                            extractor::password_required);
        }
    }
}

TEST_CASE("godot session: statically recovered sibling keys are validated against this exact pack")
{
    const test::support::temporary_directory folder("godot-session-automatic");
    const auto path = folder.path() / "game.pck";
    const auto fixture = test::support::make_key_fixture(test::support::key_shape::direct_scalar);
    write_file(path, test::support::encrypted_pck(3, true, true, fixture.expected));
    SECTION("exact executable")
    {
        write_pe(folder.path() / "game.exe", fixture);
    }
    SECTION("custom executable name")
    {
        write_pe(folder.path() / "runtime.exe", fixture);
    }
    SECTION("invalid executable is skipped before a valid sibling")
    {
        write_file(folder.path() / "a.exe", godot_bytes{'x'});
        write_pe(folder.path() / "z.exe", fixture);
    }
    SECTION("directory enumeration budget is charged before sorting")
    {
        write_pe(folder.path() / "runtime.exe", fixture);
        session_limits limits;
        limits.max_directory_entries = 0;
        CHECK_THROWS_AS(archive_session(path, {}, limits), extractor::password_required);
        return;
    }
    SECTION("executable limit is aggregate")
    {
        write_pe(folder.path() / "runtime.exe", fixture);
        session_limits limits;
        limits.max_executables = 0;
        CHECK_THROWS_AS(archive_session(path, {}, limits), extractor::password_required);
        return;
    }
    archive_session session(path);
    REQUIRE(session.key() != nullptr);
    CHECK(*session.key() == fixture.expected);
    CHECK(session.index().entries.front().logical_size == 5);
}

TEST_CASE("godot session: a pre-existing writer prevents source verification from racing mutations")
{
    const test::support::temporary_directory folder("godot-session-writer");
    const auto path = folder.path() / "game.pck";
    write_file(path, plain_pack());
    const auto handle = CreateFileW(path.c_str(), GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr,
                                    OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
    REQUIRE(handle != INVALID_HANDLE_VALUE);
    CHECK_THROWS_AS(archive_session(path), std::runtime_error);
    CHECK(CloseHandle(handle));
}

TEST_CASE("godot session: empty encrypted members never provide key evidence")
{
    const test::support::temporary_directory folder("godot-session-empty");
    test::support::godot_pck_options options;
    options.version = 3;
    test::support::godot_entry_options entry;
    entry.path = "res://empty.bin";
    entry.payload = test::support::encrypted_envelope({});
    entry.stored_size = 0;
    entry.flags = 1;
    options.entries.push_back(entry);
    const auto path = folder.path() / "game.pck";
    write_file(path, test::support::build_godot_pck(options));
    archive_session session(path, std::string(64, 'g'));
    CHECK(session.key() == nullptr);
    REQUIRE(session.index().entries.size() == 1);
    CHECK(session.index().entries.front().logical_size == 0);
    CHECK(session.index().entries.front().body.size == 40);
}

TEST_CASE("godot session: multiple archive handles keep their own streams and leases")
{
    const test::support::temporary_directory folder("godot-session-handles");
    const auto path = folder.path() / "game.pck";
    write_file(path, plain_pack());
    archive_session first(path);
    archive_session second(path);
    first.stream().seekg(2);
    second.stream().seekg(4);
    CHECK(first.stream().tellg() == 2);
    CHECK(second.stream().tellg() == 4);
    std::error_code error;
    std::filesystem::remove(path, error);
    CHECK(error);
}

TEST_CASE("godot session: optional unavailable or malformed runtimes are skipped")
{
    const test::support::temporary_directory folder("godot-session-skip-runtime");
    const auto path = folder.path() / "game.pck";
    const auto fixture = test::support::make_key_fixture(test::support::key_shape::direct_scalar);
    write_file(path, test::support::encrypted_pck(3, true, true, fixture.expected));
    SECTION("malformed PE before a valid sibling")
    {
        godot_bytes bad(63);
        bad[0] = 'M';
        bad[1] = 'Z';
        write_file(folder.path() / "a.exe", bad);
        write_pe(folder.path() / "z.EXE", fixture);
        archive_session session(path);
        REQUIRE(session.key() != nullptr);
        CHECK(*session.key() == fixture.expected);
    }
    SECTION("corrupt PE signature is skipped before a valid sibling")
    {
        auto malformed = fixture;
        malformed.image[0x80] = std::byte{'X'};
        write_pe(folder.path() / "a.exe", malformed);
        write_pe(folder.path() / "z.exe", fixture);
        archive_session session(path);
        REQUIRE(session.key() != nullptr);
        CHECK(*session.key() == fixture.expected);
        CHECK(session.index().entries.front().logical_size == 5);
    }
    SECTION("duplicate keys are deduplicated before validation")
    {
        write_pe(folder.path() / "game.exe", fixture);
        write_pe(folder.path() / "runtime.exe", fixture);
        session_limits limits;
        limits.max_validation_bytes = 64;
        archive_session session(path, {}, limits);
        REQUIRE(session.key() != nullptr);
        CHECK(*session.key() == fixture.expected);
    }
    SECTION("a candidate held by a writer cannot race key discovery")
    {
        const auto executable = folder.path() / "game.exe";
        write_pe(executable, fixture);
        const HANDLE writer = CreateFileW(executable.c_str(), GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE,
                                          nullptr, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, nullptr);
        REQUIRE(writer != INVALID_HANDLE_VALUE);
        CHECK_THROWS_AS(archive_session(path), extractor::password_required);
        CHECK(CloseHandle(writer));
    }
    SECTION("shared static scan budget cannot be refilled by the next executable")
    {
        write_pe(folder.path() / "game.exe", fixture);
        write_pe(folder.path() / "runtime.exe", fixture);
        session_limits limits;
        limits.key_scan.max_scanned_bytes = 0;
        CHECK_THROWS_AS(archive_session(path, {}, limits), extractor::password_required);
    }
}

TEST_CASE("godot session: verified-key selection rejects ambiguity without losing the admitted key")
{
    unpackrat::godot::validated_key_selection selection;
    CHECK(selection.get() == nullptr);
    const auto first = test::support::godot_test_key();
    selection.admit(first);
    REQUIRE(selection.get() != nullptr);
    CHECK(*selection.get() == first);
    selection.admit(first);
    CHECK(*selection.get() == first);
    auto different = first;
    different.front() ^= std::byte{1};
    CHECK_THROWS_AS(selection.admit(different), extractor::password_required);
    CHECK(*selection.get() == first);
}

TEST_CASE("godot session: resource presentation is snapshotted for each open archive")
{
    const test::support::temporary_directory folder("godot-session-options");
    const auto path = folder.path() / "game.pck";
    write_file(path, plain_pack());
    archive_session ordinary(path);
    archive_session decoded(path, {}, {}, true);
    CHECK(!(ordinary.decode_resources()));
    CHECK(decoded.decode_resources());
    CHECK(!(ordinary.decode_resources()));
}

TEST_CASE("godot session: embedded encrypted executables recover their own runtime key")
{
    const test::support::temporary_directory folder("godot-session-self-key");
    const auto path = folder.path() / "game.exe";
    for (const bool x64 : {false, true}) {
        CAPTURE(x64);
        const auto fixture = test::support::make_key_fixture(test::support::key_shape::direct_scalar, x64);
        godot_bytes bytes;
        for (const auto byte : fixture.image) {
            bytes.push_back(std::to_integer<std::uint8_t>(byte));
        }
        const auto pack = test::support::encrypted_pck(3, true, true, fixture.expected);
        bytes.insert(bytes.end(), pack.begin(), pack.end());
        const auto footer = bytes.size();
        bytes.resize(footer + 12);
        test::support::patch_u64(bytes, footer, pack.size());
        test::support::patch_u32(bytes, footer + 8, test::support::godot_pck_magic);
        write_file(path, bytes);
        archive_session session(path);
        CHECK(session.source_path() == path);
        REQUIRE(session.key() != nullptr);
        CHECK(*session.key() == fixture.expected);
        CHECK(session.index().entries.front().logical_size == 5);
    }
}

TEST_CASE("godot session: plain-directory key evidence chooses a bounded nonempty encrypted member")
{
    const test::support::temporary_directory folder("godot-session-evidence");
    const auto path = folder.path() / "game.pck";
    test::support::godot_pck_options options;
    options.version = 3;
    test::support::godot_entry_options ordinary;
    ordinary.path = "res://ordinary.bin";
    ordinary.payload = {'p'};
    options.entries.push_back(ordinary);
    test::support::godot_entry_options removed;
    removed.path = "res://removed.bin";
    removed.flags = 2;
    options.entries.push_back(removed);
    for (const std::size_t size : {std::size_t{0}, std::size_t{32}, std::size_t{5}, std::size_t{64}}) {
        test::support::godot_entry_options entry;
        entry.path = "res://encrypted-" + std::to_string(size) + ".bin";
        entry.stored_size = size;
        entry.flags = 1;
        entry.payload = test::support::encrypted_envelope(godot_bytes(size, 'x'));
        options.entries.push_back(entry);
    }
    write_file(path, test::support::build_godot_pck(options));
    session_limits limits;
    limits.max_validation_bytes = 56;
    archive_session session(path, hex_key(test::support::godot_test_key()), limits);
    REQUIRE(session.key() != nullptr);
    CHECK(*session.key() == test::support::godot_test_key());
    CHECK(!(session.index().key_evidence));
}

TEST_CASE("godot session: unreadable sibling listings still permit an explicit verified key")
{
    const test::support::temporary_directory folder("godot-session-no-list");
    const auto path = folder.path() / "game.pck";
    write_file(path, test::support::encrypted_pck(3, true, true));
    saved_file_acl file_acl(path);
    file_acl.protect_from_inheritance();
    saved_file_acl directory_acl(folder.path());
    directory_acl.deny_directory_listing();
    CHECK_THROWS_AS(archive_session(path), extractor::password_required);
    archive_session supplied(path, hex_key(test::support::godot_test_key()));
    REQUIRE(supplied.key() != nullptr);
    CHECK(*supplied.key() == test::support::godot_test_key());
}

TEST_CASE("godot session: a single leased handle supplies all seekable source reads")
{
    const test::support::temporary_directory folder("godot-session-one-handle");
    const auto path = folder.path() / "game.pck";
    write_file(path, plain_pack());
    DWORD before = 0;
    REQUIRE(GetProcessHandleCount(GetCurrentProcess(), &before));
    {
        archive_session session(path);
        DWORD during = 0;
        REQUIRE(GetProcessHandleCount(GetCurrentProcess(), &during));
        CHECK(during == before + 1);
        auto &source = session.stream();
        source.seekg(2);
        char byte{};
        source.read(&byte, 1);
        CHECK(byte == 'P');
        source.seekg(0, std::ios::end);
        CHECK_THROWS_AS(source.read(&byte, 1), std::ios_base::failure);
        source.clear();
        source.seekg(0);
        source.read(&byte, 1);
        CHECK(byte == 'G');
    }
    DWORD after = 0;
    REQUIRE(GetProcessHandleCount(GetCurrentProcess(), &after));
    CHECK(after == before);
}

TEST_CASE("godot session: failed CRT adoption releases its leased native handle")
{
    struct close_file final
    {
        void operator()(FILE *file) const noexcept
        {
            static_cast<void>(fclose(file));
        }
    };
    using file_owner = std::unique_ptr<FILE, close_file>;
    struct fd_owner final
    {
        int value;
        explicit fd_owner(const int descriptor) : value(descriptor)
        {
        }
        ~fd_owner()
        {
            if (value >= 0) {
                static_cast<void>(_close(value));
            }
        }
        fd_owner(const fd_owner &) = delete;
        fd_owner &operator=(const fd_owner &) = delete;
        fd_owner(fd_owner &&other) noexcept : value(std::exchange(other.value, -1))
        {
        }
        fd_owner &operator=(fd_owner &&) = delete;
    };
    struct restore_stdio_limit final
    {
        int saved = _getmaxstdio();
        restore_stdio_limit()
        {
            REQUIRE(_setmaxstdio(512) == 512);
        }
        ~restore_stdio_limit()
        {
            static_cast<void>(_setmaxstdio(saved));
        }
    };
    const test::support::temporary_directory folder("godot-session-crt-failure");
    const auto path = folder.path() / "game.pck";
    write_file(path, plain_pack());
    const restore_stdio_limit limit;
    std::vector<file_owner> files;
    std::vector<fd_owner> descriptors;
    SECTION("FILE allocation fails after fd adoption")
    {
        for (std::size_t count = 0; count < 512; ++count) {
            FILE *raw = nullptr;
            static_cast<void>(_wfopen_s(&raw, path.c_str(), L"rb"));
            file_owner file(raw);
            if (!file) {
                break;
            }
            files.push_back(std::move(file));
        }
        REQUIRE(files.size() < 512);
    }
    SECTION("fd adoption fails before native ownership transfer")
    {
        bool exhausted = false;
        for (std::size_t count = 0; count < 16'384; ++count) {
            int descriptor = -1;
            static_cast<void>(_wsopen_s(&descriptor, path.c_str(), _O_RDONLY | _O_BINARY, _SH_DENYNO, 0));
            if (descriptor < 0) {
                exhausted = true;
                break;
            }
            fd_owner adopted(descriptor);
            descriptors.push_back(std::move(adopted));
        }
        REQUIRE(exhausted);
    }
    DWORD before = 0;
    REQUIRE(GetProcessHandleCount(GetCurrentProcess(), &before));
    CHECK_THROWS_AS(archive_session(path), std::runtime_error);
    DWORD after = 0;
    REQUIRE(GetProcessHandleCount(GetCurrentProcess(), &after));
    CHECK(after == before);
}

TEST_CASE("godot session: relative pack names search only their current sibling directory")
{
    const test::support::temporary_directory folder("godot-session-relative");
    struct restore_directory final
    {
        std::filesystem::path original = std::filesystem::current_path();
        ~restore_directory()
        {
            std::error_code error;
            std::filesystem::current_path(original, error);
        }
    };
    const restore_directory restore;
    std::filesystem::current_path(folder.path());
    const auto fixture = test::support::make_key_fixture(test::support::key_shape::direct_scalar);
    write_file("game.pck", test::support::encrypted_pck(3, true, true, fixture.expected));
    write_pe("runtime.exe", fixture);
    archive_session session("game.pck");
    REQUIRE(session.key() != nullptr);
    CHECK(*session.key() == fixture.expected);
    std::filesystem::current_path(restore.original);
    auto &input = session.stream();
    input.seekg(0);
    char first{};
    input.read(&first, 1);
    CHECK(first == 'G');
}

TEST_CASE("godot session: executable-named directories never trigger recursive key discovery")
{
    const test::support::temporary_directory folder("godot-session-nonrecursive");
    const auto path = folder.path() / "game.pck";
    write_file(path, test::support::encrypted_pck(3, true, true));
    const auto nested = folder.path() / "runtime.exe";
    REQUIRE(std::filesystem::create_directory(nested));
    write_pe(nested / "game.exe", test::support::make_key_fixture(test::support::key_shape::direct_scalar));
    CHECK_THROWS_AS(archive_session(path), extractor::password_required);
}
