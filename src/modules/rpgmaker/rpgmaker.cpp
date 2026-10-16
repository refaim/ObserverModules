#include "../../core/archive_limits.h"
#include "../../core/io/bounded_stream.h"
#include "../extractor.h"

#include <array>
#include <cstdint>
#include <fstream>

// build/unpackrat-module-version.props injects the version macros from src/modules/rpgmaker/VERSION,
// the single source of truth. A project that does not import that file fails to compile here.

namespace extractor
{
    namespace
    {
        constexpr std::array<std::byte, 7> signature_prefix{
            std::byte{0x52}, std::byte{0x47}, std::byte{0x53}, std::byte{0x53},
            std::byte{0x41}, std::byte{0x44}, std::byte{0x00},
        };
        constexpr std::uint8_t legacy_version = 1;
        constexpr std::uint8_t ace_version = 3;
        constexpr std::size_t header_size = 8;
        constexpr std::uint32_t legacy_initial_key = 0xDEADCAFE;

        [[nodiscard]] bool has_signature_prefix(const std::span<const std::byte> data) noexcept
        {
            return std::equal(signature_prefix.begin(), signature_prefix.end(), data.begin());
        }

        [[nodiscard]] std::uint8_t validate_header(const std::span<const std::byte> data)
        {
            if (data.size() < header_size || !has_signature_prefix(data)) {
                throw read_error();
            }
            return std::to_integer<std::uint8_t>(data[7]);
        }

        [[nodiscard]] std::uint32_t advance_magic(uint32_t &magic) noexcept
        {
            const uint32_t old = magic;
            magic = magic * 7 + 3;
            return old;
        }

        [[nodiscard]] std::vector<std::unique_ptr<file>> list_legacy_files(unpackrat::io::bounded_stream &input)
        {
            std::vector<std::unique_ptr<file>> files;
            uint32_t key = legacy_initial_key;

            for (;;) {
                if (input.remaining() == 0) {
                    return files;
                }
                if (files.size() == unpackrat::archive_limits::max_entry_count) {
                    throw read_error();
                }

                const auto encrypted_name_length = input.read_trivial<std::uint32_t>();
                const auto name_length = encrypted_name_length ^ advance_magic(key);
                if (name_length == 0 || name_length > unpackrat::archive_limits::max_path_bytes ||
                    static_cast<std::uint64_t>(name_length) > static_cast<std::uint64_t>(input.remaining())) {
                    throw read_error();
                }

                std::vector<char> name_buf(name_length);
                input.read_exact(name_buf.data(), name_buf.size());
                for (size_t i = 0; i < name_buf.size(); ++i) {
                    const auto encrypted_byte = static_cast<unsigned char>(name_buf[i]);
                    const auto key_byte = static_cast<unsigned char>(advance_magic(key));
                    name_buf[i] = static_cast<char>(encrypted_byte ^ key_byte);
                }
                if (std::find(name_buf.begin(), name_buf.end(), '\0') != name_buf.end()) {
                    throw read_error();
                }

                const auto encrypted_size = input.read_trivial<std::uint32_t>();
                const auto body_size = encrypted_size ^ advance_magic(key);
                const auto body_offset = input.position();
                if (static_cast<std::uint64_t>(body_size) > static_cast<std::uint64_t>(input.remaining())) {
                    throw read_error();
                }

                auto new_file = std::make_unique<file>();
                new_file->path = std::string(name_buf.data(), name_buf.size());
                new_file->offset = body_offset;
                new_file->compressed_body_size_in_bytes = body_size;
                new_file->uncompressed_body_size_in_bytes = body_size;
                new_file->magic = key;
                files.push_back(std::move(new_file));

                // The body key is frozen per entry; skipping the body must not advance metadata keys.
                input.seek_absolute(body_offset + static_cast<std::streamoff>(body_size));
            }
        }

        [[nodiscard]] std::vector<std::unique_ptr<file>> list_ace_files(unpackrat::io::bounded_stream &input)
        {
            const auto archive_size = input.size();

            std::vector<std::unique_ptr<file>> files;
            const uint32_t magic = input.read_trivial<std::uint32_t>() * 9 + 3;
            for (;;) {
                const uint32_t offset = input.read_trivial<std::uint32_t>() ^ magic;
                if (offset == 0)
                    break;

                const uint32_t size = input.read_trivial<std::uint32_t>() ^ magic;
                const uint32_t file_magic = input.read_trivial<std::uint32_t>() ^ magic;
                const uint32_t name_len = input.read_trivial<std::uint32_t>() ^ magic;

                const auto name_position = input.position();
                if (name_len > unpackrat::archive_limits::max_path_bytes ||
                    static_cast<std::uint64_t>(name_len) > static_cast<std::uint64_t>(archive_size - name_position)) {
                    throw read_error();
                }

                std::vector<char> name_buf(name_len);
                input.read_exact(name_buf.data(), name_buf.size());
                for (size_t i = 0; i < name_len; ++i) {
                    name_buf[i] = static_cast<char>(static_cast<unsigned char>(name_buf[i]) ^
                                                    static_cast<unsigned char>(magic >> (8 * (i % 4))));
                }

                auto new_file = std::make_unique<file>();
                new_file->path = std::string(name_buf.data(), name_len);
                new_file->offset = offset;
                new_file->compressed_body_size_in_bytes = size;
                new_file->uncompressed_body_size_in_bytes = size;
                new_file->magic = file_magic;
                files.push_back(std::move(new_file));
            }
            return files;
        }
    } // namespace
    void configure([[maybe_unused]] const wchar_t *settings) noexcept
    {
    }
    void reset_settings() noexcept
    {
    }

    version_info get_version_info() noexcept
    {
        return {
            {0xc4674077, 0x464a, 0x425b, {0x89, 0x80, 0x9e, 0x14, 0xe8, 0x16, 0x49, 0x00}},
            UNPACKRAT_MODULE_VERSION_MAJOR,
            UNPACKRAT_MODULE_VERSION_MINOR,
        };
    }

    std::vector<std::byte> extractor::get_signature()
    {
        return {signature_prefix.begin(), signature_prefix.end()};
    }

    archive_info extractor::get_archive_info(
        [[maybe_unused]] std::istream &stream, const std::span<const std::byte> &data,
        [[maybe_unused]] const std::filesystem::path &path,
        [[maybe_unused]] const std::string_view password) // NOLINT(*-convert-member-functions-to-static)
    {
        if (data.empty()) {
            return archive_info{L"RPG Maker", L"-", L"RPG Maker archive"};
        }

        const auto version = validate_header(data);
        if (version == legacy_version) {
            return archive_info{L"RGSS", L"-", L"RPG Maker XP/VX"};
        }
        if (version == ace_version) {
            return archive_info{L"RGSS3", L"-", L"RPG Maker VX Ace"};
        }
        throw read_error();
    }

    std::vector<std::unique_ptr<file>> extractor::list_files(
        std::istream &stream) // NOLINT(*-convert-member-functions-to-static)
    {
        unpackrat::io::bounded_stream input(stream);
        // A repeated listing must restart at the header; an empty archive caches no items, so the
        // wrapper re-parses and the stream would otherwise still sit where the last listing stopped.
        input.seek_absolute(0);
        std::array<std::byte, header_size> header{};
        input.read_exact(reinterpret_cast<char *>(header.data()), header.size());

        const auto version = validate_header(header);
        if (version == legacy_version) {
            return list_legacy_files(input);
        }
        if (version == ace_version) {
            return list_ace_files(input);
        }
        throw read_error();
    }

    uint32_t extractor::decrypt(uint32_t magic,
                                std::vector<char> &data) const // NOLINT(*-convert-member-functions-to-static)
    {
        const size_t size = data.size();
        size_t i = 0;

        while (i + 4 <= size) {
            uint32_t value;
            std::memcpy(&value, &data[i], sizeof(value));
            value ^= advance_magic(magic);
            std::memcpy(&data[i], &value, sizeof(value));
            i += 4;
        }

        while (i < size) {
            const auto byte_value = static_cast<uint8_t>(magic >> (i % 4 * 8));
            data[i] = static_cast<char>(static_cast<uint8_t>(data[i]) ^ byte_value);
            ++i;
        }

        return magic;
    }
} // namespace extractor
