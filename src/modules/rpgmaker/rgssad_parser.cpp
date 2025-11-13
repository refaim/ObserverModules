#include "rgss_parser.h"

namespace extractor
{
    rgssad_parser::rgssad_parser(const uint8_t version) : version_(version)
    {
    }

    uint32_t rgssad_parser::advance_magic(uint32_t &magic) const
    {
        const uint32_t old = magic;
        magic = magic * 7 + 3;
        return old;
    }

    std::vector<std::unique_ptr<file> > rgssad_parser::list_files(std::ifstream &stream)
    {
        stream.seekg(8);
        std::vector<std::unique_ptr<file> > files;
        uint32_t magic = 0xDEADCAFE;

        while (true) {
            uint32_t name_len;
            if (!stream.read(reinterpret_cast<char*>(&name_len), sizeof(name_len))) break;
            name_len ^= advance_magic(magic);

            std::vector<char> name_buf(name_len);
            stream.read(name_buf.data(), name_len);
            for (size_t i = 0; i < name_len; ++i) {
                name_buf[i] = static_cast<char>(
                    static_cast<unsigned char>(name_buf[i]) ^
                    static_cast<unsigned char>(advance_magic(magic)));
                if (name_buf[i] == '\\') name_buf[i] = '/';
            }

            uint32_t size;
            if (!stream.read(reinterpret_cast<char*>(&size), sizeof(size))) break;
            size ^= advance_magic(magic);

            const uint32_t offset = static_cast<uint32_t>(stream.tellg());
            const uint32_t file_magic = magic;

            stream.seekg(size, std::ios::cur);

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

    archive_info rgssad_parser::get_archive_info() const
    {
        switch (version_) {
            case 1:
                return archive_info{L"RGSSAD", L"-", L"RPG Maker XP"};
            case 2:
                return archive_info{L"RGSS2A", L"-", L"RPG Maker VX"};
            default:
                throw read_error();
        }
    }
}