#include "../extractor.h"

#include <fstream>

namespace extractor
{
    version_info get_version_info() noexcept
    {
        return {
            {0x83ccd102, 0xac5f, 0x45df, {0xa4, 0xc1, 0x1f, 0x4a, 0xb0, 0x5a, 0xde, 0xd8}},
            1, 0,
        };
    }

    std::vector<std::byte> extractor::get_signature() noexcept
    {
        return {std::byte{'G'}, std::byte{'D'}, std::byte{'P'}, std::byte{'C'}};
    }

    // ReSharper disable once CppMemberFunctionMayBeStatic
    archive_info extractor::get_archive_info(const std::span<const std::byte> &data) noexcept // NOLINT(*-convert-member-functions-to-static)
    {
        return archive_info{L"Godot", L"-", L"Godot Engine Package"};
    }

    static uint32_t read_u32(std::ifstream &stream)
    {
        uint32_t value;
        stream.read(reinterpret_cast<char *>(&value), sizeof(value));
        if (stream.fail()) {
            throw read_error();
        }
        return value;
    }

    static int32_t read_i32(std::ifstream &stream)
    {
        int32_t value;
        stream.read(reinterpret_cast<char *>(&value), sizeof(value));
        if (stream.fail()) {
            throw read_error();
        }
        return value;
    }

    static int64_t read_i64(std::ifstream &stream)
    {
        int64_t value;
        stream.read(reinterpret_cast<char *>(&value), sizeof(value));
        if (stream.fail()) {
            throw read_error();
        }
        return value;
    }

    // ReSharper disable once CppMemberFunctionMayBeStatic
    std::vector<std::unique_ptr<file> > extractor::list_files(std::ifstream &stream) // NOLINT(*-convert-member-functions-to-static)
    {
        stream.seekg(static_cast<std::streamoff>(get_signature().size()));

        const int32_t pack_version = read_i32(stream);
        if (pack_version < 0 || pack_version > 2) {
            throw read_error();
        }

        // Read engine version (major, minor, patch)
        read_i32(stream);
        read_i32(stream);
        read_i32(stream);

        int64_t files_base_offset = 0;
        if (pack_version == 2) {
            const uint32_t pack_flags = read_u32(stream);
            if ((pack_flags & 1) != 0) {
                throw read_error(); // Encrypted archives not supported
            }
            files_base_offset = read_i64(stream);
        }

        constexpr int32_t reserved_size = 64;
        stream.seekg(reserved_size, std::ios::cur);

        const int32_t file_count = read_i32(stream);
        if (file_count < 0 || file_count > 1000000) {
            throw read_error();
        }

        std::vector<std::unique_ptr<file> > files;
        files.reserve(file_count);
        for (int32_t i = 0; i < file_count; ++i) {
            const int32_t path_len = read_i32(stream);
            if (path_len <= 0 || path_len > 4096) {
                throw read_error();
            }

            std::string path;
            path.resize(path_len);
            stream.read(path.data(), path_len);
            if (stream.fail()) {
                throw read_error();
            }

            const int64_t offset = read_i64(stream);
            const int64_t size = read_i64(stream);

            constexpr int32_t md5_size = 16;
            stream.seekg(md5_size, std::ios::cur);

            if (pack_version == 2) {
                const uint32_t file_flags = read_u32(stream);
                if ((file_flags & 1) != 0) {
                    throw read_error(); // Encrypted files not supported
                }
            }

            // Remove Godot path prefixes
            constexpr std::string_view prefixes[] = {"res://", "user://"};
            for (const auto &prefix: prefixes) {
                if (path.starts_with(prefix)) {
                    path.erase(0, prefix.size());
                    break;
                }
            }

            auto new_file = std::make_unique<file>();
            new_file->path = path;
            new_file->offset = pack_version == 2 ? offset + files_base_offset : offset;
            new_file->compressed_body_size_in_bytes = size;
            new_file->uncompressed_body_size_in_bytes = size;

            files.push_back(std::move(new_file));
        }

        return files;
    }

    uint32_t extractor::decrypt(uint32_t magic, std::vector<char> &data) const // NOLINT(*-convert-member-functions-to-static)
    {
        return magic;
    }
}
