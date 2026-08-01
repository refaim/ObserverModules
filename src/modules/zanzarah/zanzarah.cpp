#include "../../core/archive_limits.h"
#include "../../core/io/bounded_stream.h"
#include "../extractor.h"

#include <algorithm>
#include <fstream>

namespace extractor
{
    version_info get_version_info() noexcept
    {
        return {
            {0x86e7e4c3, 0xbc44, 0x4e8e, {0x90, 0xaf, 0xbd, 0xbd, 0x1c, 0xb6, 0x1a, 0x83}},
            2,
            0,
        };
    }

    std::vector<std::byte> extractor::get_signature()
    {
        return {std::byte{0}, std::byte{0}, std::byte{0}, std::byte{0}};
    }

    archive_info extractor::get_archive_info(
        const std::span<const std::byte> &data) // NOLINT(*-convert-member-functions-to-static)
    {
        static_cast<void>(data);
        return archive_info{L"Zanzarah", L"", L""};
    }

    int32_t read_positive_or_zero_int32(observer::io::bounded_stream &stream)
    {
        const auto value = stream.read_trivial<std::int32_t>();

        if (value < 0) {
            throw read_error();
        }

        return value;
    }

    int32_t read_positive_int32(observer::io::bounded_stream &stream)
    {
        const int32_t value = read_positive_or_zero_int32(stream);
        if (value == 0) {
            throw read_error();
        }
        return value;
    }

    std::vector<std::unique_ptr<file>> extractor::list_files(
        std::istream &stream) // NOLINT(*-convert-member-functions-to-static)
    {
        observer::io::bounded_stream input(stream);
        input.seek_absolute(static_cast<std::streamoff>(get_signature().size()));
        const auto archive_size = input.size();
        const auto file_count = static_cast<std::size_t>(read_positive_int32(input));
        constexpr std::size_t minimum_entry_metadata_bytes = sizeof(std::int32_t) * 3 + 1;
        const auto remaining_bytes = static_cast<std::uint64_t>(input.remaining());
        if (file_count > observer::archive_limits::max_entry_count ||
            file_count > remaining_bytes / minimum_entry_metadata_bytes) {
            throw read_error();
        }

        auto files = std::vector<std::unique_ptr<file>>();
        files.reserve(file_count);

        std::string path;
        for (std::size_t i = 0; i < file_count; ++i) {
            const auto path_len = static_cast<std::size_t>(read_positive_int32(input));
            if (path_len > observer::archive_limits::max_path_bytes ||
                path_len > static_cast<std::uint64_t>(input.remaining())) {
                throw read_error();
            }
            path.resize(path_len);
            input.read_exact(path.data(), path.size());

            const auto block_offset = read_positive_or_zero_int32(input);
            const auto block_size = read_positive_int32(input);

            constexpr int32_t attr_size = 4;
            if (block_size < attr_size) {
                throw read_error();
            }

            auto new_file = std::make_unique<file>();
            new_file->path = path;
            new_file->offset = static_cast<std::int64_t>(block_offset) + attr_size;
            new_file->compressed_body_size_in_bytes = static_cast<std::int64_t>(block_size) - attr_size;
            new_file->uncompressed_body_size_in_bytes = new_file->compressed_body_size_in_bytes;

            files.push_back(std::move(new_file));
        }

        const auto body_position = input.position();
        const auto body_bytes = static_cast<std::int64_t>(archive_size - body_position);
        for (const auto &file : files) {
            if (file->offset > body_bytes || file->compressed_body_size_in_bytes > body_bytes - file->offset) {
                throw read_error();
            }
            file->offset += body_position;
            if (constexpr std::string_view relative_prefix = "..\\"; file->path.starts_with(relative_prefix)) {
                file->path.erase(0, relative_prefix.size());
            }
        }

        return files;
    }

    uint32_t extractor::decrypt(uint32_t magic, std::vector<char> &data) const
    {
        static_cast<void>(data);
        return magic;
    }
} // namespace extractor
