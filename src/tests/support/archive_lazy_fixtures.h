#ifndef UNPACKRAT_TESTS_SUPPORT_ARCHIVE_LAZY_FIXTURES_H
#define UNPACKRAT_TESTS_SUPPORT_ARCHIVE_LAZY_FIXTURES_H

#include "modules/extractor.h"

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <span>
#include <string>
#include <string_view>
#include <vector>

namespace test::support
{
    // A single fake entry the test-only extractor functions expose.
    struct fake_file_definition final
    {
        std::string path;
        std::string header;
        std::int64_t offset = 0;
        std::int64_t compressed_body_size_in_bytes = 0;
        std::int64_t uncompressed_body_size_in_bytes = 0;
        std::uint32_t magic = 0;
        std::function<void(std::istream &, std::ostream &, const std::function<void(std::int64_t)> &)> extract;
    };

    // Process-wide state the test-only extractor functions read. Reset explicitly by
    // installing a fake_extractor_scope, which restores the previous state on exit.
    struct fake_extractor_state final
    {
        std::vector<std::byte> signature{std::byte{'F'}, std::byte{'A'}, std::byte{'K'}, std::byte{'E'}};
        extractor::archive_info archive_info{L"FakeFormat", L"-", L"fake comment"};
        std::vector<fake_file_definition> files;
        bool list_files_read_error = false;
        std::function<std::unique_ptr<extractor::storage_state>()> storage_factory;
        int archive_info_calls = 0;
        int list_files_calls = 0;
        std::size_t archive_info_data_size = 0;
        std::filesystem::path archive_info_path;
        std::string archive_info_password;
        std::streamoff archive_info_stream_size = -1;
    };

    // RAII installation of a fake_extractor_state; restores the previous state on exit.
    class fake_extractor_scope final
    {
      public:
        explicit fake_extractor_scope(fake_extractor_state &state) noexcept;
        ~fake_extractor_scope();

        fake_extractor_scope(const fake_extractor_scope &) = delete;
        fake_extractor_scope &operator=(const fake_extractor_scope &) = delete;

      private:
        fake_extractor_state *previous_;
    };

    // Owns a temporary file holding the given contents until destruction.
    class temporary_file final
    {
      public:
        temporary_file(std::string_view label, std::span<const std::uint8_t> contents);
        ~temporary_file();

        temporary_file(const temporary_file &) = delete;
        temporary_file &operator=(const temporary_file &) = delete;

        [[nodiscard]] const std::filesystem::path &path() const noexcept;

      private:
        std::filesystem::path path_;
    };

    // Owns a temporary directory until destruction.
    class temporary_directory final
    {
      public:
        explicit temporary_directory(std::string_view label);
        ~temporary_directory();

        temporary_directory(const temporary_directory &) = delete;
        temporary_directory &operator=(const temporary_directory &) = delete;

        [[nodiscard]] const std::filesystem::path &path() const noexcept;

      private:
        std::filesystem::path path_;
    };

    // A unique temporary path that is not created.
    [[nodiscard]] std::filesystem::path unused_path(std::string_view label);
} // namespace test::support
#endif
