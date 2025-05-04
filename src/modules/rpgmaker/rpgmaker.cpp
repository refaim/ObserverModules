#include "../extractor.h"

#include <fstream>

namespace extractor
{
    version_info get_version_info() noexcept
    {
        return {
            {0xc4674077, 0x464a, 0x425b, {0x89, 0x80, 0x9e, 0x14, 0xe8, 0x16, 0x49, 0x00}},
            1, 0,
        };
    }

    std::vector<std::byte> extractor::get_signature() noexcept
    {
        const std::string str = "RGSSAD";
        std::vector<std::byte> signature(str.size());
        std::memcpy(signature.data(), str.data(), str.size());
        signature.push_back(std::byte{0});
        signature.push_back(std::byte{3});
        return signature;
    }

    // ReSharper disable once CppMemberFunctionMayBeStatic
    archive_info extractor::get_archive_info(const std::span<const std::byte> &data) noexcept // NOLINT(*-convert-member-functions-to-static)
    {
        // TODO implement: RGSS => RPG Maker XP, 0xDEADCAFE, XOR => zlib
        // TODO implement: RGSS2 => RPG Maker VX, 0xCAFEDEAD, XOR => zlib
        // TODO RPG Maker MV
        // TODO RPG Maker MZ
        // TODO .rvdata, .rvdata2
        return archive_info{L"RGSS3", L"DEFLATE", L"RPG Maker VX Ace"};
    }

    static uint32_t read_u32(std::ifstream &stream)
    {
        uint32_t value;
        stream.read(reinterpret_cast<char *>(&value), sizeof(value));
        return value;
    }

    // ReSharper disable once CppMemberFunctionMayBeStatic
    std::vector<std::unique_ptr<file> > extractor::list_files(std::ifstream &stream) // NOLINT(*-convert-member-functions-to-static)
    {
        std::vector<std::unique_ptr<file> > files;
        const uint32_t magic = read_u32(stream) * 9 + 3;
        while (true) {
            const uint32_t offset = read_u32(stream) ^ magic;
            if (offset == 0) break;

            const uint32_t size = read_u32(stream) ^ magic;
            const uint32_t file_magic = read_u32(stream) ^ magic;
            const uint32_t name_len = read_u32(stream) ^ magic;

            std::vector<char> name_buf(name_len);
            stream.read(name_buf.data(), name_len);
            for (size_t i = 0; i < name_len; ++i) {
                name_buf[i] = static_cast<char>(
                    static_cast<unsigned char>(name_buf[i]) ^
                    static_cast<unsigned char>(magic >> (8 * (i % 4))));
            }

            auto new_file = std::make_unique<file>();
            new_file->path = std::string(name_buf.data(), name_len);
            new_file->offset = offset;
            new_file->compressed_body_size_in_bytes = size;
            new_file->uncompressed_body_size_in_bytes = size;
            files.push_back(std::move(new_file));
        }
        return files;
    }
}
