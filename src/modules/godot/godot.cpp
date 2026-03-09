#include "../extractor.h"

#include <algorithm>
#include <fstream>
#include <regex>
#include <unordered_map>
#include <unordered_set>

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

    static std::string read_file_content(std::ifstream &stream, int64_t offset, int64_t size)
    {
        const auto old_pos = stream.tellg();
        stream.seekg(offset);

        std::string content;
        content.resize(size);
        stream.read(content.data(), size);

        stream.seekg(old_pos);

        if (stream.fail()) {
            throw read_error();
        }

        return content;
    }

    static std::string parse_remap_path(const std::string &content)
    {
        const std::regex path_regex("path=\"([^\"]+?)\"");
        std::smatch match;

        if (!std::regex_search(content, match, path_regex) || match.empty()) {
            throw read_error();
        }

        return match[1].str();
    }

    static std::string strip_godot_prefix(const std::string &path)
    {
        constexpr std::string_view prefixes[] = {"res://", "user://"};
        for (const auto &prefix: prefixes) {
            if (path.starts_with(prefix)) {
                return path.substr(prefix.size());
            }
        }
        return path;
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

            path = strip_godot_prefix(path);

            auto new_file = std::make_unique<file>();
            new_file->path = path;
            new_file->offset = pack_version == 2 ? offset + files_base_offset : offset;
            new_file->compressed_body_size_in_bytes = size;
            new_file->uncompressed_body_size_in_bytes = size;

            files.push_back(std::move(new_file));
        }

        // Build path map for quick lookup
        std::unordered_map<std::string, file *> path_map;
        for (const auto &f: files) {
            path_map[f->path] = f.get();
        }

        // Find and process .import/.remap files
        constexpr std::string_view import_ext = ".import";
        constexpr std::string_view remap_ext = ".remap";

        std::vector<std::unique_ptr<file> > virtual_files;
        std::unordered_set<file *> files_to_remove;

        for (const auto &f: files) {
            if (f->path.ends_with(import_ext) || f->path.ends_with(remap_ext)) {
                const std::string content = read_file_content(stream, f->offset, f->compressed_body_size_in_bytes);
                const std::string remap_path = strip_godot_prefix(parse_remap_path(content));

                const auto it = path_map.find(remap_path);
                if (it == path_map.end()) {
                    // File not found in .godot, keep .import/.remap as is
                    continue;
                }
                file *actual_file = it->second;

                auto virtual_file = std::make_unique<file>();
                std::string virtual_path = f->path;
                if (virtual_path.ends_with(import_ext)) {
                    virtual_path = virtual_path.substr(0, virtual_path.size() - import_ext.size());
                } else if (virtual_path.ends_with(remap_ext)) {
                    virtual_path = virtual_path.substr(0, virtual_path.size() - remap_ext.size());
                }

                // Get extension from actual file (e.g., .ctex, .fontdata)
                const size_t dot_pos = remap_path.rfind('.');
                if (dot_pos != std::string::npos) {
                    virtual_path += remap_path.substr(dot_pos);
                }

                virtual_file->path = virtual_path;
                virtual_file->offset = actual_file->offset;
                virtual_file->compressed_body_size_in_bytes = actual_file->compressed_body_size_in_bytes;
                virtual_file->uncompressed_body_size_in_bytes = actual_file->uncompressed_body_size_in_bytes;
                virtual_file->magic = actual_file->magic;
                virtual_files.push_back(std::move(virtual_file));

                // Mark metadata and actual file for removal
                files_to_remove.insert(f.get());
                files_to_remove.insert(actual_file);
            }
        }

        std::erase_if(files, [&files_to_remove](const std::unique_ptr<file> &f)
        {
            return files_to_remove.contains(f.get());
        });

        for (auto &vf: virtual_files) {
            files.push_back(std::move(vf));
        }

        return files;
    }

    // ReSharper disable once CppMemberFunctionMayBeStatic
    uint32_t extractor::decrypt(uint32_t magic, std::vector<char> &data) const // NOLINT(*-convert-member-functions-to-static)
    {
        return magic;
    }
}
