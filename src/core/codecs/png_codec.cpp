#include "png_codec.h"

#include <cstdint>
#include <exception>
#include <limits>
#include <memory>
#include <string>

// libspng is compiled into this module as a static port, so its declarations must not carry the
// dllimport decoration. The macro stays private to this translation unit.
#define SPNG_STATIC
#include <spng.h>

namespace unpackrat::codecs
{
    namespace
    {
        constexpr std::uint64_t rgba_channels = 4;

        // Carries live state across the C streaming callbacks. The sink reference is non-owning;
        // the surrounding encode_png owns it for the whole call. written tracks the bytes already
        // handed to the sink so the byte budget can be enforced before each write; failure records
        // the first C++ exception raised by the sink so it can be rethrown once control is back in
        // C++ code.
        struct stream_state final
        {
            stream_state(const std::function<void(std::span<const std::byte>)> &output, const std::uint64_t max_bytes)
                : sink(output), budget(max_bytes)
            {
            }

            const std::function<void(std::span<const std::byte>)> &sink;
            std::uint64_t budget = 0;
            std::uint64_t written = 0;
            bool budget_exceeded = false;
            std::exception_ptr failure;
        };

        // libspng's write callback. It is noexcept because it is invoked from C: it must never let
        // a C++ exception cross the library boundary. A breached budget or an exception from the
        // sink is captured and reported to libspng as an I/O error; encode_png rethrows the
        // original exception once the library call returns.
        int SPNG_CDECL write_png_chunk(spng_ctx *, void *user, void *source, const std::size_t length) noexcept
        {
            auto &state = *static_cast<stream_state *>(user);
            if (length > state.budget - state.written) {
                state.budget_exceeded = true;
                return SPNG_IO_ERROR;
            }
            try {
                state.sink(std::span<const std::byte>(static_cast<const std::byte *>(source), length));
            } catch (...) {
                state.failure = std::current_exception();
                return SPNG_IO_ERROR;
            }
            state.written += length;
            return SPNG_OK;
        }

        std::uint64_t validate_geometry(const std::uint32_t width, const std::uint32_t height,
                                        const image_limits &limits)
        {
            if (width == 0 || height == 0) {
                throw image_error("PNG dimensions must be non-zero");
            }
            const std::uint64_t pixels = static_cast<std::uint64_t>(width) * height;
            if (pixels > limits.max_pixels) {
                throw image_error("PNG image exceeds the configured pixel budget");
            }
            if (pixels > (std::numeric_limits<std::uint64_t>::max)() / rgba_channels) {
                throw image_error("PNG dimensions overflow the byte count");
            }
            return pixels * rgba_channels;
        }

        using png_context = std::unique_ptr<spng_ctx, decltype(&spng_ctx_free)>;

        png_context create_encoder(stream_state &state, const std::uint32_t width, const std::uint32_t height)
        {
            png_context context(spng_ctx_new(SPNG_CTX_ENCODER), &spng_ctx_free);
            if (context == nullptr) {
                throw image_error("failed to create the PNG encoder context");
            }
            spng_ihdr header{};
            header.width = width;
            header.height = height;
            header.bit_depth = 8;
            header.color_type = SPNG_COLOR_TYPE_TRUECOLOR_ALPHA;
            header.compression_method = 0;
            header.filter_method = 0;
            header.interlace_method = 0;
            // A rejected format dimension is reported by encode_image as a missing valid IHDR.
            // The fresh context and callback remain local to this adapter throughout encoding.
            static_cast<void>(spng_set_png_stream(context.get(), &write_png_chunk, &state));
            static_cast<void>(spng_set_ihdr(context.get(), &header));
            return context;
        }

        [[noreturn]] void encoding_error(const int result, const stream_state &state)
        {
            if (state.failure) {
                std::rethrow_exception(state.failure);
            }
            if (state.budget_exceeded) {
                // Construct the diagnostic only after returning from the noexcept C callback.
                throw image_error("PNG output exceeds the configured byte budget");
            }
            throw image_error(std::string("PNG encoding failed: ").append(spng_strerror(result)));
        }
    } // namespace

    void encode_png(const rgba_image &image, const std::function<void(std::span<const std::byte>)> &sink,
                    const image_limits &limits)
    {
        const std::uint64_t raw_bytes = validate_geometry(image.width, image.height, limits);
        if (raw_bytes != image.pixels.size()) {
            throw image_error("PNG pixel buffer does not match the declared dimensions");
        }
        stream_state state{sink, limits.max_output_bytes};
        const auto context = create_encoder(state, image.width, image.height);
        const int result = spng_encode_image(context.get(), image.pixels.data(), image.pixels.size(), SPNG_FMT_PNG,
                                             SPNG_ENCODE_FINALIZE);
        if (result != SPNG_OK) {
            encoding_error(result, state);
        }
    }

    void encode_png_rows(const std::uint32_t width, const std::uint32_t height,
                         const std::function<void(std::uint32_t, std::span<std::byte>)> &read_row,
                         const std::function<void(std::span<const std::byte>)> &sink, const image_limits &limits)
    {
        static_cast<void>(validate_geometry(width, height, limits));
        const std::uint64_t row_bytes = static_cast<std::uint64_t>(width) * rgba_channels;
        if constexpr (sizeof(std::size_t) < sizeof(std::uint64_t)) {
            if (row_bytes > (std::numeric_limits<std::size_t>::max)()) {
                throw image_error("PNG row byte count is not addressable");
            }
        }
        stream_state state{sink, limits.max_output_bytes};
        const auto context = create_encoder(state, width, height);
        const int initialized =
            spng_encode_image(context.get(), nullptr, 0, SPNG_FMT_PNG, SPNG_ENCODE_PROGRESSIVE | SPNG_ENCODE_FINALIZE);
        if (initialized != SPNG_OK) {
            encoding_error(initialized, state);
        }
        std::vector<std::byte> row(static_cast<std::size_t>(row_bytes));
        for (std::uint32_t index = 0; index < height; ++index) {
            read_row(index, row);
            const int result = spng_encode_row(context.get(), row.data(), row.size());
            if (result != (index + 1 == height ? SPNG_EOI : SPNG_OK)) {
                encoding_error(result, state);
            }
        }
    }
} // namespace unpackrat::codecs
