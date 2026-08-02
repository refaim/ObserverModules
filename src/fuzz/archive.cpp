#include "../modules/extractor.h"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

namespace
{
    std::uint32_t initial_magic(const std::uint8_t *data, const std::size_t size) noexcept
    {
        std::uint32_t magic = 0;
        if (size > 0) {
            std::memcpy(&magic, data, std::min(size, sizeof(magic)));
        }
        return magic;
    }
} // namespace

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t *data, const std::size_t size)
{
    extractor::extractor parser;

    std::vector<char> body(size);
    if (size > 0) {
        std::memcpy(body.data(), data, size);
    }
    static_cast<void>(parser.decrypt(initial_magic(data, size), body));

    const std::string bytes(reinterpret_cast<const char *>(data), size);
    std::istringstream stream(bytes, std::ios::in | std::ios::binary);

    try {
        static_cast<void>(parser.list_files(stream));
    } catch (const std::invalid_argument &) {
        // Invalid numeric fields are expected.
        return 0;
    } catch (const std::out_of_range &) {
        // Invalid numeric fields are expected. std::length_error is deliberately not caught.
        return 0;
    } catch (const std::runtime_error &) {
        // Malformed/truncated archive input is expected. Allocation failures must still escape.
        return 0;
    }

    return 0;
}
