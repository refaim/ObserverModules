#include "rgss_parser.h"

namespace extractor
{
    uint32_t rgss_parser::read_u32(std::ifstream &stream)
    {
        uint32_t value;
        stream.read(reinterpret_cast<char *>(&value), sizeof(value));
        return value;
    }

    uint32_t rgss_parser::decrypt(uint32_t magic, std::vector<char> &data) const
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
            data[i] = static_cast<char>(static_cast<uint8_t>(data[i]) ^ static_cast<uint8_t>(magic >> ((i % 4) * 8)));
            ++i;
        }

        return magic;
    }
}