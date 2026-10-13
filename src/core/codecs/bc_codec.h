#pragma once

#include "image_types.h"

#include <cstdint>
#include <span>

namespace unpackrat::codecs
{
    // Decodes a block-compressed texture payload into an 8-bit RGBA image. The block layout,
    // the required input length and the output budgets are validated before anything is decoded,
    // so a mismatched or oversized payload is rejected instead of read out of bounds. BC4 and BC5
    // expand their single- and two-channel data to (R, 0, 0, 255) and (R, G, 0, 255); no normal
    // map reconstruction is performed.
    [[nodiscard]] rgba_image decode_bc(std::span<const std::byte> input, bc_format format, std::uint32_t width,
                                       std::uint32_t height, const image_limits &limits = {});
} // namespace unpackrat::codecs
