#include "zlib_codec.h"

#include <algorithm>
#include <string>

#include <zlib.h>

namespace observer::compression
{
    namespace
    {
        class inflate_context final
        {
          public:
            inflate_context()
            {
                if (inflateInit(&stream_) != Z_OK) {
                    throw error("Failed to initialize zlib decompression");
                }
            }

            ~inflate_context()
            {
                static_cast<void>(inflateEnd(&stream_));
            }

            inflate_context(const inflate_context &) = delete;
            inflate_context &operator=(const inflate_context &) = delete;
            inflate_context(inflate_context &&) = delete;
            inflate_context &operator=(inflate_context &&) = delete;

            [[nodiscard]] z_stream &stream() noexcept
            {
                return stream_;
            }

          private:
            z_stream stream_{};
        };

        [[noreturn]] void throw_zlib_error(const char *operation, const z_stream &stream)
        {
            auto message = std::string(operation);
            if (stream.msg != nullptr) {
                message.append(": ").append(stream.msg);
            }
            throw error(message);
        }
    } // namespace

    std::vector<std::byte> decompress_zlib(const std::span<const std::byte> input, const std::size_t max_output_bytes)
    {
        inflate_context context;
        auto &stream = context.stream();
        std::vector<std::byte> output;
        std::vector<std::byte> chunk(std::size_t{64} * 1024);
        std::size_t input_offset = 0;

        while (true) {
            if (stream.avail_in == 0 && input_offset < input.size()) {
                const auto input_size =
                    std::min(input.size() - input_offset, static_cast<std::size_t>(std::numeric_limits<uInt>::max()));
                stream.next_in = reinterpret_cast<Bytef *>(const_cast<std::byte *>(input.data() + input_offset));
                stream.avail_in = static_cast<uInt>(input_size);
                input_offset += input_size;
            }

            stream.next_out = reinterpret_cast<Bytef *>(chunk.data());
            stream.avail_out = static_cast<uInt>(chunk.size());
            const auto result = inflate(&stream, Z_NO_FLUSH);
            const auto produced = chunk.size() - stream.avail_out;
            if (produced > max_output_bytes - output.size()) {
                throw error("zlib output exceeds the configured metadata budget");
            }
            output.insert(output.end(), chunk.begin(), chunk.begin() + static_cast<std::ptrdiff_t>(produced));

            if (result == Z_STREAM_END) {
                const auto consumed_input = input_offset - stream.avail_in;
                if (consumed_input != input.size()) {
                    throw error("Trailing data after zlib stream");
                }
                return output;
            }
            if (result == Z_BUF_ERROR) {
                throw error("Truncated zlib stream");
            }
            if (result != Z_OK) {
                throw_zlib_error("zlib decompression failed", stream);
            }
        }
    }
} // namespace observer::compression
