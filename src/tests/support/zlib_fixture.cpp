#include "zlib_fixture.h"

#include <limits>
#include <stdexcept>

#include <zlib.h>

namespace test::support
{
    std::vector<std::byte> compress_zlib_fixture(const std::span<const std::byte> input)
    {
        if (input.size() > std::numeric_limits<uLong>::max()) {
            throw std::runtime_error("Fixture input is too large for zlib compression");
        }

        auto output_size = compressBound(static_cast<uLong>(input.size()));
        std::vector<std::byte> output(output_size);
        const auto result =
            compress2(reinterpret_cast<Bytef *>(output.data()), &output_size,
                      reinterpret_cast<const Bytef *>(input.data()), static_cast<uLong>(input.size()), Z_BEST_SPEED);
        if (result != Z_OK) {
            throw std::runtime_error("Failed to compress zlib fixture");
        }
        output.resize(output_size);
        return output;
    }
} // namespace test::support
