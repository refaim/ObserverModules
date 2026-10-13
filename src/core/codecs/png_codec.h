#pragma once

#include "image_types.h"

#include <cstddef>
#include <functional>
#include <span>

namespace unpackrat::codecs
{
    // Streams image as an 8-bit RGBA PNG to sink. The image buffer, its dimensions and the
    // configured limits are validated before any library call; sink is invoked zero or more times
    // with successive pieces of the encoded stream. On any failure - an invalid image, a breached
    // limit or an exception thrown by sink - image_error, or the sink's own exception, is thrown
    // and the stream is left incomplete.
    void encode_png(const rgba_image &image, const std::function<void(std::span<const std::byte>)> &sink,
                    const image_limits &limits = {});
    // Pulls one complete RGBA scanline at a time; working storage is proportional to width.
    void encode_png_rows(std::uint32_t width, std::uint32_t height,
                         const std::function<void(std::uint32_t, std::span<std::byte>)> &read_row,
                         const std::function<void(std::span<const std::byte>)> &sink, const image_limits &limits = {});
} // namespace unpackrat::codecs
