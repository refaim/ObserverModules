#include "../extractor.h"
#include "rgss_parser.h"

#include <fstream>
#include <memory>

namespace extractor
{
    version_info get_version_info() noexcept
    {
        return {
            {0xc4674077, 0x464a, 0x425b, {0x89, 0x80, 0x9e, 0x14, 0xe8, 0x16, 0x49, 0x00}},
            1, 0,
        };
    }

    class extractor final
    {
    private:
        std::unique_ptr<rgss_parser> parser_;

    public:
        std::vector<std::byte> get_signature() noexcept
        {
            const std::string str = "RGSSAD";
            std::vector<std::byte> signature(str.size());
            std::memcpy(signature.data(), str.data(), str.size());
            signature.push_back(std::byte{0});
            return signature;
        }

        archive_info get_archive_info(const std::span<const std::byte> &data) noexcept
        {
            if (data.size() < 8) {
                throw read_error();
            }
            
            const uint8_t version = static_cast<uint8_t>(data[7]);
            switch (version) {
                case 1:
                case 2:
                    parser_ = std::make_unique<rgssad_parser>(version);
                    break;
                case 3:
                    parser_ = std::make_unique<rgss3a_parser>();
                    break;
                default:
                    throw read_error();
            }
            
            return parser_->get_archive_info();
        }

        std::vector<std::unique_ptr<file> > list_files(std::ifstream &stream)
        {
            if (!parser_) {
                throw read_error();
            }
            return parser_->list_files(stream);
        }

        uint32_t decrypt(uint32_t magic, std::vector<char> &data) const
        {
            if (!parser_) {
                throw read_error();
            }
            return parser_->decrypt(magic, data);
        }
    };
}
