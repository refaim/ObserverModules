#ifndef UNPACKRAT_CORE_CODECS_IMAGE_TYPES_H
#define UNPACKRAT_CORE_CODECS_IMAGE_TYPES_H

#include <cstddef>
#include <cstdint>
#include <limits>
#include <stdexcept>
#include <vector>

// Platform-neutral value types shared by the image codec adapters. Nothing here depends on a
// third-party header: external types, error codes and context ownership stay inside each
// adapter's implementation file.
namespace unpackrat::codecs
{
    // A decoded 8-bit-per-channel RGBA image. pixels holds exactly width * height * 4 bytes in
    // row-major order; the two dimensions and the buffer are kept together so a malformed image
    // cannot be described partially.
    struct rgba_image
    {
        std::uint32_t width = 0;
        std::uint32_t height = 0;
        std::vector<std::byte> pixels;
    };

    // Optional caller bounds every codec enforces before it allocates or calls a library.
    // Defaults permit every representable stream; buffer arithmetic and format bounds still apply.
    // max_pixels bounds width * height; max_output_bytes bounds the bytes this codec produces
    // (the encoded stream for the PNG encoder, the decoded RGBA buffer for the BC decoder).
    struct image_limits
    {
        std::uint64_t max_pixels = (std::numeric_limits<std::uint64_t>::max)();
        std::uint64_t max_output_bytes = (std::numeric_limits<std::uint64_t>::max)();
    };

    // Raised for any rejected image input: a malformed description, a breached limit, a checked
    // size overflow or a library failure. It carries a diagnostic message like std::runtime_error.
    class image_error final : public std::runtime_error
    {
      public:
        using std::runtime_error::runtime_error;
    };

    // The block-compression families this adapter decodes. BC6H (HDR), and the Basis/ETC/ASTC/
    // PVRTC families are deliberately absent until they are implemented and verified.
    enum class bc_format : std::uint8_t
    {
        bc1,
        bc2,
        bc3,
        bc4,
        bc5,
        bc7,
    };
} // namespace unpackrat::codecs
#endif
