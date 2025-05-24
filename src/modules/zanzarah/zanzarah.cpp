#include "../extractor.h"

#include <algorithm>
#include <fstream>

namespace extractor
{
    version_info get_version_info() noexcept
    {
        return {
            {0x86e7e4c3, 0xbc44, 0x4e8e, {0x90, 0xaf, 0xbd, 0xbd, 0x1c, 0xb6, 0x1a, 0x83}},
            2, 0,
        };
    }

    std::vector<std::byte> extractor::get_signature() noexcept
    {
        return {std::byte{0}, std::byte{0}, std::byte{0}, std::byte{0}};
    }

    // ReSharper disable once CppMemberFunctionMayBeStatic
    archive_info extractor::get_archive_info(const std::span<const std::byte> &data) noexcept // NOLINT(*-convert-member-functions-to-static)
    {
        return archive_info{L"Zanzarah", L"", L""};
    }

    int32_t read_positive_or_zero_int32(std::ifstream &stream)
    {
        int32_t value;

        try {
            stream.read(reinterpret_cast<char *>(&value), sizeof(value));
        } catch (std::ios_base::failure &) {
            throw read_error();
        }

        if (value < 0) {
            throw read_error();
        }

        return value;
    }

    int32_t read_positive_int32(std::ifstream &stream)
    {
        const int32_t value = read_positive_or_zero_int32(stream);
        if (value == 0) {
            throw read_error();
        }
        return value;
    }

    // ReSharper disable once CppMemberFunctionMayBeStatic
    std::vector<std::unique_ptr<file> > extractor::list_files(std::ifstream &stream) // NOLINT(*-convert-member-functions-to-static)
    {
        auto files = std::vector<std::unique_ptr<file> >();
        files.reserve(read_positive_int32(stream));

        std::string path;
        for (size_t i = 0; i < files.capacity(); ++i) {
            const auto path_len = read_positive_int32(stream);
            path.resize(path_len);
            stream.read(path.data(), path_len);

            const auto block_offset = read_positive_or_zero_int32(stream);
            const auto block_size = read_positive_int32(stream);

            constexpr int32_t attr_size = 4;

            auto new_file = std::make_unique<file>();
            new_file->path = path;
            new_file->offset = block_offset + attr_size;
            new_file->compressed_body_size_in_bytes = block_size - attr_size;
            new_file->uncompressed_body_size_in_bytes = new_file->compressed_body_size_in_bytes;

            files.push_back(std::move(new_file));
        }

        for (const auto &file: files) {
            file->offset += stream.tellg();
            if (constexpr std::string_view relative_prefix = "..\\"; file->path.starts_with(relative_prefix)) {
                file->path.erase(0, relative_prefix.size());
            }
        }

        return files;
    }
}
