#include "../../core/archive_limits.h"
#include "../../core/io/bounded_stream.h"
#include "../extractor.h"

#include <fstream>

namespace extractor
{
    version_info get_version_info() noexcept
    {
        return {
            {0xc4674077, 0x464a, 0x425b, {0x89, 0x80, 0x9e, 0x14, 0xe8, 0x16, 0x49, 0x00}},
            1,
            0,
        };
    }

    std::vector<std::byte> extractor::get_signature()
    {
        const std::string str = "RGSSAD";
        std::vector<std::byte> signature(str.size());
        std::memcpy(signature.data(), str.data(), str.size());
        signature.push_back(std::byte{0});
        signature.push_back(std::byte{3});
        return signature;
    }

    archive_info extractor::get_archive_info(
        const std::span<const std::byte> &data) // NOLINT(*-convert-member-functions-to-static)
    {
        static_cast<void>(data);
        return archive_info{L"RGSS3", L"-", L"RPG Maker VX Ace"};
    }

    std::vector<std::unique_ptr<file>> extractor::list_files(
        std::istream &stream) // NOLINT(*-convert-member-functions-to-static)
    {
        observer::io::bounded_stream input(stream);
        input.seek_absolute(static_cast<std::streamoff>(get_signature().size()));
        const auto archive_size = input.size();

        std::vector<std::unique_ptr<file>> files;
        const uint32_t magic = input.read_trivial<std::uint32_t>() * 9 + 3;
        while (true) {
            const uint32_t offset = input.read_trivial<std::uint32_t>() ^ magic;
            if (offset == 0)
                break;

            const uint32_t size = input.read_trivial<std::uint32_t>() ^ magic;
            const uint32_t file_magic = input.read_trivial<std::uint32_t>() ^ magic;
            const uint32_t name_len = input.read_trivial<std::uint32_t>() ^ magic;

            const auto name_position = input.position();
            if (name_len > observer::archive_limits::max_path_bytes ||
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

    static uint32_t advance_magic(uint32_t &magic)
    {
        const uint32_t old = magic;
        magic = magic * 7 + 3;
        return old;
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
