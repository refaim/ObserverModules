#include "rgss_parser.h"

namespace extractor
{
    uint32_t rgss3a_parser::advance_magic(uint32_t &magic) const
    {
        const uint32_t old = magic;
        magic = magic * 9 + 3;
        return old;
    }

    std::vector<std::unique_ptr<file> > rgss3a_parser::list_files(std::ifstream &stream)
    {
        stream.seekg(8);
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
                if (name_buf[i] == '\\') name_buf[i] = '/';
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

    archive_info rgss3a_parser::get_archive_info() const
    {
        return archive_info{L"RGSS3A", L"-", L"RPG Maker VX Ace"};
    }
}