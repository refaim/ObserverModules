#include "archive_fixtures.h"
#include "zlib_fixture.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <format>
#include <fstream>
#include <iterator>
#include <limits>
#include <stdexcept>
#include <string>

#include <windows.h>
#include <winioctl.h>

namespace test::support
{
    namespace
    {
        class unique_handle final
        {
          public:
            explicit unique_handle(const HANDLE value) noexcept : value_(value)
            {
            }

            ~unique_handle()
            {
                if (value_ != INVALID_HANDLE_VALUE) {
                    static_cast<void>(CloseHandle(value_));
                }
            }

            unique_handle(const unique_handle &) = delete;
            unique_handle &operator=(const unique_handle &) = delete;

            [[nodiscard]] HANDLE get() const noexcept
            {
                return value_;
            }

          private:
            HANDLE value_;
        };

        class temporary_path_guard final
        {
          public:
            explicit temporary_path_guard(const std::filesystem::path &path) noexcept : path_(path)
            {
            }

            ~temporary_path_guard()
            {
                if (!retained_) {
                    std::error_code error;
                    std::filesystem::remove(path_, error);
                }
            }

            temporary_path_guard(const temporary_path_guard &) = delete;
            temporary_path_guard &operator=(const temporary_path_guard &) = delete;

            void retain() noexcept
            {
                retained_ = true;
            }

          private:
            const std::filesystem::path &path_;
            bool retained_ = false;
        };

        void append_u32(byte_buffer &output, const std::uint32_t value)
        {
            output.push_back(static_cast<std::uint8_t>(value));
            output.push_back(static_cast<std::uint8_t>(value >> 8));
            output.push_back(static_cast<std::uint8_t>(value >> 16));
            output.push_back(static_cast<std::uint8_t>(value >> 24));
        }

        void append_bytes(byte_buffer &output, const std::string_view value)
        {
            output.reserve(output.size() + value.size());
            std::ranges::transform(value, std::back_inserter(output), [](const char character) {
                return static_cast<std::uint8_t>(static_cast<unsigned char>(character));
            });
        }

        [[nodiscard]] std::uint32_t checked_u32(const std::size_t value, const std::string_view description)
        {
            if (value > std::numeric_limits<std::uint32_t>::max()) {
                throw std::runtime_error(std::format("{} does not fit in an archive field", description));
            }
            return static_cast<std::uint32_t>(value);
        }

        [[nodiscard]] std::uint32_t checked_u32_sum(const std::size_t value, const std::size_t increment,
                                                    const std::string_view description)
        {
            if (value > std::numeric_limits<std::uint32_t>::max() - increment) {
                throw std::runtime_error(std::format("{} does not fit in an archive field", description));
            }
            return static_cast<std::uint32_t>(value + increment);
        }

        [[nodiscard]] std::uint8_t checked_u8(const std::size_t value, const std::string_view description)
        {
            if (value > std::numeric_limits<std::uint8_t>::max()) {
                throw std::runtime_error(std::format("{} does not fit in the minimal Ren'Py fixture", description));
            }
            return static_cast<std::uint8_t>(value);
        }

        [[nodiscard]] byte_buffer build_renpy_archive(const std::span<const std::uint8_t> pickle_index,
                                                      const std::string_view payload, const renpy_version version,
                                                      const std::uint8_t encryption_key, const std::size_t data_offset)
        {
            const auto pickle_bytes = std::as_bytes(pickle_index);
            const auto compressed = compress_zlib_fixture(pickle_bytes);

            const auto index_offset = data_offset + payload.size();
            byte_buffer archive;
            if (version == renpy_version::rpa_2_0) {
                append_bytes(archive, std::format("RPA-2.0 {:016x}\n", index_offset));
            } else {
                append_bytes(archive, std::format("RPA-3.0 {:016x} {:08x}\n", index_offset, encryption_key));
            }
            if (archive.size() > data_offset) {
                throw std::runtime_error("Ren'Py fixture data offset overlaps its header");
            }
            archive.resize(data_offset, 0);
            append_bytes(archive, payload);
            std::ranges::transform(compressed, std::back_inserter(archive),
                                   [](const auto byte) { return std::to_integer<std::uint8_t>(byte); });
            return archive;
        }
    } // namespace

    temporary_archive::temporary_archive(const std::string_view label, const std::span<const std::uint8_t> contents)
    {
        if (contents.size() > static_cast<std::size_t>(std::numeric_limits<std::streamsize>::max())) {
            throw std::runtime_error("Fixture is too large for std::ofstream");
        }

        static std::atomic_uint32_t sequence = 0;
        path_ = std::filesystem::temp_directory_path() /
                std::format("observer-modules-{}-{}-{}.bin", label, GetCurrentProcessId(), sequence.fetch_add(1));

        std::ofstream output(path_, std::ios::binary | std::ios::trunc);
        if (!output.is_open()) {
            throw std::runtime_error(std::format("Failed to create fixture: {}", path_.string()));
        }
        output.write(reinterpret_cast<const char *>(contents.data()), static_cast<std::streamsize>(contents.size()));
        if (!output.good()) {
            output.close();
            std::error_code error;
            std::filesystem::remove(path_, error);
            throw std::runtime_error(std::format("Failed to write fixture: {}", path_.string()));
        }
    }

    temporary_archive::~temporary_archive()
    {
        std::error_code error;
        std::filesystem::remove(path_, error);
    }

    const std::filesystem::path &temporary_archive::path() const noexcept
    {
        return path_;
    }

    temporary_sparse_renpy_archive::temporary_sparse_renpy_archive()
    {
        static std::atomic_uint32_t sequence = 0;
        path_ = std::filesystem::temp_directory_path() /
                std::format("observer-modules-renpy-sparse-{}-{}.bin", GetCurrentProcessId(), sequence.fetch_add(1));
        temporary_path_guard path_guard(path_);

        const unique_handle output(
            CreateFileW(path_.c_str(), GENERIC_WRITE, 0, nullptr, CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr));
        if (output.get() == INVALID_HANDLE_VALUE) {
            throw std::runtime_error(
                std::format("Failed to create sparse Ren'Py fixture (Win32 error {})", GetLastError()));
        }

        DWORD bytes_returned = 0;
        if (DeviceIoControl(output.get(), FSCTL_SET_SPARSE, nullptr, 0, nullptr, 0, &bytes_returned, nullptr) ==
            FALSE) {
            throw std::runtime_error(
                std::format("Failed to mark a Ren'Py fixture sparse (Win32 error {})", GetLastError()));
        }

        constexpr std::string_view header_text = "RPA-2.0 0000000000000020\n";
        std::array<char, 32> header{};
        std::ranges::copy(header_text, header.begin());
        DWORD bytes_written = 0;
        if (WriteFile(output.get(), header.data(), static_cast<DWORD>(header.size()), &bytes_written, nullptr) ==
                FALSE ||
            bytes_written != header.size()) {
            throw std::runtime_error(
                std::format("Failed to write a sparse Ren'Py fixture (Win32 error {})", GetLastError()));
        }

        constexpr LONGLONG max_compressed_index_size = 64LL * 1024 * 1024;
        const LARGE_INTEGER logical_end{.QuadPart =
                                            static_cast<LONGLONG>(header.size()) + max_compressed_index_size + 1};
        if (SetFilePointerEx(output.get(), logical_end, nullptr, FILE_BEGIN) == FALSE ||
            SetEndOfFile(output.get()) == FALSE) {
            throw std::runtime_error(
                std::format("Failed to size a sparse Ren'Py fixture (Win32 error {})", GetLastError()));
        }
        path_guard.retain();
    }

    temporary_sparse_renpy_archive::~temporary_sparse_renpy_archive()
    {
        std::error_code error;
        std::filesystem::remove(path_, error);
    }

    const std::filesystem::path &temporary_sparse_renpy_archive::path() const noexcept
    {
        return path_;
    }

    byte_buffer make_zanzarah_archive(const std::string_view path, const std::string_view payload)
    {
        byte_buffer archive(4, 0);
        append_u32(archive, 1);
        append_u32(archive, checked_u32(path.size(), "Zanzarah path length"));
        append_bytes(archive, path);
        append_u32(archive, 0);
        append_u32(archive, checked_u32_sum(payload.size(), 4, "Zanzarah payload length"));
        append_u32(archive, 0x12345678);
        append_bytes(archive, payload);
        return archive;
    }

    byte_buffer make_zanzarah_archive_with_entries(const std::size_t entry_count)
    {
        constexpr std::size_t block_size = 5;
        if (entry_count == 0 || entry_count > static_cast<std::size_t>(std::numeric_limits<std::int32_t>::max()) ||
            entry_count > static_cast<std::size_t>(std::numeric_limits<std::int32_t>::max()) / block_size) {
            throw std::runtime_error("Zanzarah fixture entry count is outside the representable range");
        }

        byte_buffer archive(4, 0);
        append_u32(archive, checked_u32(entry_count, "Zanzarah entry count"));
        for (std::size_t index = 0; index < entry_count; ++index) {
            const auto path = std::format("data/file-{:06}.bin", index);
            append_u32(archive, checked_u32(path.size(), "Zanzarah path length"));
            append_bytes(archive, path);
            append_u32(archive, checked_u32(index * block_size, "Zanzarah block offset"));
            append_u32(archive, static_cast<std::uint32_t>(block_size));
        }

        for (std::size_t index = 0; index < entry_count; ++index) {
            append_u32(archive, 0x12345678);
            archive.push_back(static_cast<std::uint8_t>('a' + index % 26));
        }
        return archive;
    }

    byte_buffer make_rpgmaker_archive(const std::string_view path, const std::string_view payload)
    {
        byte_buffer archive{'R', 'G', 'S', 'S', 'A', 'D', 0, 3};
        constexpr std::uint32_t seed = 1;
        constexpr std::uint32_t file_magic = 0x12345678;
        constexpr std::uint32_t table_magic = seed * 9 + 3;
        const auto data_offset = checked_u32_sum(path.size(), 32, "RPG Maker data offset");

        append_u32(archive, seed);
        append_u32(archive, data_offset ^ table_magic);
        append_u32(archive, checked_u32(payload.size(), "RPG Maker payload length") ^ table_magic);
        append_u32(archive, file_magic ^ table_magic);
        append_u32(archive, checked_u32(path.size(), "RPG Maker path length") ^ table_magic);
        for (std::size_t index = 0; index < path.size(); ++index) {
            const auto key = static_cast<std::uint8_t>(table_magic >> (8 * (index % 4)));
            archive.push_back(static_cast<std::uint8_t>(static_cast<unsigned char>(path[index])) ^ key);
        }
        append_u32(archive, table_magic);

        byte_buffer encrypted;
        encrypted.reserve(payload.size());
        append_bytes(encrypted, payload);
        auto magic = file_magic;
        std::size_t index = 0;
        while (index + 4 <= encrypted.size()) {
            for (std::size_t byte_index = 0; byte_index < 4; ++byte_index) {
                encrypted[index + byte_index] ^= static_cast<std::uint8_t>(magic >> (8 * byte_index));
            }
            magic = magic * 7 + 3;
            index += 4;
        }
        while (index < encrypted.size()) {
            encrypted[index] ^= static_cast<std::uint8_t>(magic >> (8 * (index % 4)));
            ++index;
        }
        archive.insert(archive.end(), encrypted.begin(), encrypted.end());
        return archive;
    }

    byte_buffer make_renpy_archive(const std::string_view path, const std::string_view payload)
    {
        return make_renpy_archive(path, payload, {});
    }

    byte_buffer make_renpy_archive(const std::string_view path, const std::string_view payload,
                                   const renpy_archive_options &options)
    {
        constexpr std::uint8_t tuple2 = 0x86;
        constexpr std::uint8_t tuple3 = 0x87;
        const std::size_t data_offset = options.version == renpy_version::rpa_2_0 ? 32 : 64;
        const std::size_t header_size = options.header ? options.header->size() : 0;

        const auto encoded_path_size = checked_u8(path.size(), "path length");
        const auto encoded_header_size = checked_u8(header_size, "header length");
        auto encoded_offset = checked_u8(data_offset, "data offset");
        auto encoded_body_size = checked_u8(payload.size() + header_size, "body length");
        if (options.version == renpy_version::rpa_3_0) {
            encoded_offset ^= options.encryption_key;
            encoded_body_size ^= options.encryption_key;
        }

        byte_buffer pickle;
        pickle.push_back('}');
        pickle.push_back('U');
        pickle.push_back(encoded_path_size);
        append_bytes(pickle, path);
        pickle.push_back(']');
        pickle.push_back('K');
        pickle.push_back(encoded_offset);
        pickle.push_back('K');
        pickle.push_back(encoded_body_size);
        if (options.header) {
            pickle.push_back('C');
            pickle.push_back(encoded_header_size);
            append_bytes(pickle, *options.header);
        } else if (options.include_none_header) {
            pickle.push_back('N');
        }
        pickle.push_back(options.header || options.include_none_header ? tuple3 : tuple2);
        pickle.push_back('a');
        pickle.push_back('s');
        pickle.push_back('.');

        return build_renpy_archive(pickle, payload, options.version, options.encryption_key, data_offset);
    }

    byte_buffer make_renpy_archive_with_index(const std::span<const std::uint8_t> pickle_index,
                                              const std::string_view payload)
    {
        return build_renpy_archive(pickle_index, payload, renpy_version::rpa_2_0, 0, 32);
    }

    byte_buffer make_renpy_archive_with_expanded_index(const std::size_t expanded_size)
    {
        if (expanded_size == 0) {
            throw std::runtime_error("Expanded Ren'Py index fixture must not be empty");
        }
        return make_renpy_archive_with_index(byte_buffer(expanded_size, static_cast<std::uint8_t>('N')));
    }
} // namespace test::support
