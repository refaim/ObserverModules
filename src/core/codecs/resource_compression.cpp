#include "resource_compression.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <memory>
#include <vector>

#include <brotli/decode.h>
#include <fastlz.h>
#include <zlib.h>
#include <zstd.h>

namespace unpackrat::codecs
{
    namespace
    {
        using bytes = std::vector<std::byte>;

        constexpr std::size_t stream_chunk_bytes = std::size_t{64} * 1024;
        constexpr std::size_t int_max_bytes = static_cast<std::size_t>(std::numeric_limits<int>::max());
        constexpr std::size_t fastlz_minimum_output_bytes = 16;

        std::size_t require_zstd_result(const std::size_t result, const char *message)
        {
            if (ZSTD_isError(result))
                throw compression_error(message);
            return result;
        }

        // Bounds the input and the requested output by both the caller budget and the
        // widest `int`/`uInt` the underlying codecs accept, so every later narrowing
        // cast is a checked value.
        void require_block_budget(const std::size_t input_bytes, const std::size_t expected_size,
                                  const std::size_t max_output_bytes)
        {
            const std::size_t limit = std::min(max_output_bytes, int_max_bytes);
            if (input_bytes > limit || expected_size > limit) {
                throw compression_error("compressed block exceeds the configured byte budget");
            }
        }

        // FastLZ encodes through `int` lengths and requires at least 16 writable output
        // bytes even when the logical block is shorter; the reference pads short inputs
        // on compression, so a decoded short block must expand to exactly the padded 16
        // bytes and the adapter then keeps only the requested logical prefix.
        //
        // FastLZ's public decode API reports only the produced length, never the number of
        // input bytes it consumed, and its inner loop deliberately stops once fewer than
        // two input bytes remain. A block with trailing or padding bytes therefore still
        // decodes "successfully". Because `decompress_block` promises that a corrupt,
        // truncated or trailing-padded stream is rejected, the adapter must independently
        // prove that the whole block was consumed before handing it to the library.
        //
        // This checked walk replays the exact input cursor that the pinned upstream
        // level-1 and level-2 decoders advance over their control bytes, literal runs,
        // length escapes and offset/far-distance operands (ariya/FastLZ
        // b1342dabcf5257ab303743c9332fe75e9147a011, fastlz.c), then requires the cursor to
        // land exactly on the end of the block. The graph is faithful to the upstream
        // grammar; the actual decompressor stays upstream, so this is only the required
        // consumption guarantee and not a second decoder.
        //
        // The reference decoders accumulate a length escape into a 32-bit `len`, so a run
        // of 0xFF continuation bytes can wrap that counter and collapse a match that
        // logically copies more than 2^32 bytes into a short copy that happens to fill the
        // exact requested buffer. The walk therefore accumulates every escape and every
        // token with checked 64-bit arithmetic and rejects any running total that would
        // exceed the buffer the caller is about to hand upstream, before the library runs.
        [[noreturn]] void throw_fastlz_stream_error(const char *message)
        {
            throw compression_error(message);
        }

        void require_complete_fastlz_input(const std::span<const std::byte> input, const std::size_t output_bound)
        {
            const auto limit = static_cast<std::ptrdiff_t>(input.size());
            const std::ptrdiff_t ip_bound = limit - 2; // upstream `ip_limit - 2`
            const auto byte_at = [&input](const std::ptrdiff_t index) {
                return std::to_integer<std::uint8_t>(input[static_cast<std::size_t>(index)]);
            };

            const std::uint64_t bound = output_bound;
            const auto require_output = [bound](const std::uint64_t amount) {
                if (amount > bound) {
                    throw_fastlz_stream_error("fastlz block expands beyond the expected output");
                }
            };

            // The compression level is encoded in the top three bits of the first byte.
            const std::uint32_t level = (byte_at(0) >> 5U) + 1U;
            if (level != 1U && level != 2U) {
                throw_fastlz_stream_error("fastlz block has an unknown level");
            }

            std::uint64_t produced = 0;
            std::ptrdiff_t ip = 1;
            std::uint32_t ctrl = byte_at(0) & 31U;
            while (true) {
                if (ctrl >= 32U) {
                    std::uint64_t len = static_cast<std::uint64_t>((ctrl >> 5U) - 1U);
                    const std::uint32_t ofs = (ctrl & 31U) << 8U;
                    if (len == 7U - 1U) { // length escape
                        if (level == 2U) {
                            std::uint8_t code = 0;
                            do {
                                if (ip > ip_bound) {
                                    throw_fastlz_stream_error("fastlz block is truncated");
                                }
                                code = byte_at(ip++);
                                len += code;
                                require_output(len);
                            } while (code == 255U);
                        } else {
                            if (ip > ip_bound) {
                                throw_fastlz_stream_error("fastlz block is truncated");
                            }
                            len += byte_at(ip++);
                            require_output(len);
                        }
                    }
                    if (ip >= limit) {
                        throw_fastlz_stream_error("fastlz block is truncated");
                    }
                    const std::uint8_t code = byte_at(ip++);
                    if (level == 2U && code == 255U && ofs == (31U << 8U)) {
                        if (ip >= ip_bound) {
                            throw_fastlz_stream_error("fastlz block is truncated");
                        }
                        ip += 2;
                    }
                    len += 3U;
                    produced += len;
                    require_output(produced);
                } else {
                    const std::ptrdiff_t run = static_cast<std::ptrdiff_t>(ctrl) + 1;
                    if (ip + run > limit) {
                        throw_fastlz_stream_error("fastlz block is truncated");
                    }
                    ip += run;
                    produced += static_cast<std::uint64_t>(run);
                    require_output(produced);
                }

                if (level == 1U) {
                    if (ip > ip_bound) {
                        break;
                    }
                } else if (ip >= limit) {
                    break;
                }
                ctrl = byte_at(ip++);
            }

            if (ip != limit) {
                throw_fastlz_stream_error("trailing data after compressed block");
            }
        }

        void decode_fastlz(const std::span<const std::byte> input, const std::size_t expected_size, bytes &output)
        {
            if (input.empty()) {
                throw compression_error("fastlz block is empty");
            }
            require_complete_fastlz_input(input, std::max(expected_size, fastlz_minimum_output_bytes));
            const int input_bytes = static_cast<int>(input.size());
            if (expected_size >= fastlz_minimum_output_bytes) {
                output.resize(expected_size);
                const int produced =
                    fastlz_decompress(input.data(), input_bytes, output.data(), static_cast<int>(expected_size));
                if (produced == 0) {
                    throw compression_error("fastlz block is truncated or corrupt");
                }
                if (static_cast<std::size_t>(produced) != expected_size) {
                    throw compression_error("fastlz block output size mismatch");
                }
                return;
            }

            std::array<std::byte, fastlz_minimum_output_bytes> scratch{};
            const int produced =
                fastlz_decompress(input.data(), input_bytes, scratch.data(), static_cast<int>(scratch.size()));
            if (produced == 0) {
                throw compression_error("fastlz block is truncated or corrupt");
            }
            if (static_cast<std::size_t>(produced) != scratch.size()) {
                throw compression_error("fastlz block output size mismatch");
            }
            output.assign(scratch.begin(), scratch.begin() + static_cast<std::ptrdiff_t>(expected_size));
        }

        class inflate_context final
        {
          public:
            explicit inflate_context(const int window_bits)
            {
                if (inflateInit2(&stream_, window_bits) != Z_OK) {
                    throw compression_error("failed to initialize zlib decompression");
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

        // zlib-wrapped DEFLATE (window_bits 15) and GZip (window_bits 15 + 16).
        void decode_zlib_stream(const std::span<const std::byte> input, const std::size_t expected_size,
                                const int window_bits, bytes &output)
        {
            inflate_context context(window_bits);
            auto &stream = context.stream();
            std::vector<std::byte> chunk(stream_chunk_bytes);
            std::size_t input_offset = 0;

            while (true) {
                if (stream.avail_in == 0 && input_offset < input.size()) {
                    const auto feed = std::min(input.size() - input_offset,
                                               static_cast<std::size_t>(std::numeric_limits<uInt>::max()));
                    stream.next_in = reinterpret_cast<Bytef *>(const_cast<std::byte *>(input.data() + input_offset));
                    stream.avail_in = static_cast<uInt>(feed);
                    input_offset += feed;
                }

                stream.next_out = reinterpret_cast<Bytef *>(chunk.data());
                stream.avail_out = static_cast<uInt>(chunk.size());
                const int result = inflate(&stream, Z_NO_FLUSH);
                const auto produced = chunk.size() - static_cast<std::size_t>(stream.avail_out);
                if (produced > expected_size - output.size()) {
                    throw compression_error("compressed block output size mismatch");
                }
                output.insert(output.end(), chunk.begin(), chunk.begin() + static_cast<std::ptrdiff_t>(produced));

                if (result == Z_STREAM_END) {
                    const auto consumed = input_offset - static_cast<std::size_t>(stream.avail_in);
                    if (consumed != input.size()) {
                        throw compression_error("trailing data after compressed block");
                    }
                    if (output.size() != expected_size) {
                        throw compression_error("compressed block output size mismatch");
                    }
                    return;
                }
                if (result == Z_BUF_ERROR) {
                    throw compression_error("truncated compressed block");
                }
                if (result != Z_OK) {
                    throw compression_error("zlib decompression failed");
                }
            }
        }

        void decode_zstd(const std::span<const std::byte> input, const std::size_t expected_size, bytes &output)
        {
            const std::size_t frame_bytes = require_zstd_result(
                ZSTD_findFrameCompressedSize(input.data(), input.size()), "zstd block frame is malformed");
            if (frame_bytes != input.size()) {
                throw compression_error("trailing data after compressed block");
            }
            output.resize(std::max(expected_size, std::size_t{1}));
            const std::size_t produced = require_zstd_result(
                ZSTD_decompress(output.data(), expected_size, input.data(), input.size()), "zstd decompression failed");
            if (produced != expected_size) {
                throw compression_error("compressed block output size mismatch");
            }
            output.resize(expected_size);
        }

        class brotli_context final
        {
          public:
            brotli_context() : state_(BrotliDecoderCreateInstance(nullptr, nullptr, nullptr))
            {
                if (state_ == nullptr) {
                    throw compression_error("failed to initialize brotli decompression");
                }
            }

            ~brotli_context()
            {
                BrotliDecoderDestroyInstance(state_);
            }

            brotli_context(const brotli_context &) = delete;
            brotli_context &operator=(const brotli_context &) = delete;
            brotli_context(brotli_context &&) = delete;
            brotli_context &operator=(brotli_context &&) = delete;

            [[nodiscard]] BrotliDecoderState *state() const noexcept
            {
                return state_;
            }

          private:
            BrotliDecoderState *state_;
        };

        void decode_brotli(const std::span<const std::byte> input, const std::size_t expected_size, bytes &output)
        {
            brotli_context context;
            const auto *next_in = reinterpret_cast<const std::uint8_t *>(input.data());
            std::size_t avail_in = input.size();
            std::size_t total_out = 0;
            std::vector<std::byte> chunk(stream_chunk_bytes);

            while (true) {
                auto *next_out = reinterpret_cast<std::uint8_t *>(chunk.data());
                std::size_t avail_out = chunk.size();
                const BrotliDecoderResult result = BrotliDecoderDecompressStream(context.state(), &avail_in, &next_in,
                                                                                 &avail_out, &next_out, &total_out);
                const auto produced = chunk.size() - avail_out;
                if (produced > expected_size - output.size()) {
                    throw compression_error("compressed block output size mismatch");
                }
                output.insert(output.end(), chunk.begin(), chunk.begin() + static_cast<std::ptrdiff_t>(produced));

                if (result == BROTLI_DECODER_RESULT_SUCCESS) {
                    if (avail_in != 0) {
                        throw compression_error("trailing data after compressed block");
                    }
                    if (output.size() != expected_size) {
                        throw compression_error("compressed block output size mismatch");
                    }
                    return;
                }
                if (result == BROTLI_DECODER_RESULT_ERROR) {
                    throw compression_error("brotli decompression failed");
                }
                if (result == BROTLI_DECODER_RESULT_NEEDS_MORE_INPUT) {
                    throw compression_error("truncated compressed block");
                }
            }
        }
    } // namespace

    std::vector<std::byte> decompress_block(const std::span<const std::byte> input, const compression_kind kind,
                                            const std::size_t expected_size, const std::size_t max_output_bytes)
    {
        require_block_budget(input.size(), expected_size, max_output_bytes);

        bytes output;
        switch (kind) {
        case compression_kind::fastlz:
            decode_fastlz(input, expected_size, output);
            break;
        case compression_kind::deflate:
            decode_zlib_stream(input, expected_size, 15, output);
            break;
        case compression_kind::gzip:
            decode_zlib_stream(input, expected_size, 15 + 16, output);
            break;
        case compression_kind::zstd:
            decode_zstd(input, expected_size, output);
            break;
        case compression_kind::brotli:
            decode_brotli(input, expected_size, output);
            break;
        default:
            throw compression_error("unsupported compression mode");
        }
        return output;
    }
} // namespace unpackrat::codecs

namespace unpackrat::codecs
{
    struct block_stream_decoder::state final
    {
        compression_kind kind;
        std::uint64_t compressed_size;
        std::uint64_t logical_size;
        std::function<void(std::uint64_t, std::span<std::byte>)> source;
        std::array<std::byte, 65536> input{};
        std::array<std::byte, 65536> output{};
        std::size_t input_count = 0;
        std::size_t input_pos = 0;
        std::uint64_t physical_read = 0;
        std::uint64_t decoded = 0;
        std::uint64_t window_start = 0;
        std::size_t window_size = 0;
        bool complete = false;
        z_stream z{};
        bool z_ready = false;
        ZSTD_DStream *zstd = nullptr;
        BrotliDecoderState *brotli = nullptr;
        // The pinned FastLZ API only decodes into a complete output buffer. Its level-2 far
        // reference can look back at most 65,535 + 8,191 + 1 bytes, so this ring suffices for
        // overlapping matches without retaining a wide RSCC block.
        std::array<std::byte, 73728> fast_history{};
        std::uint64_t fast_pending = 0;
        std::uint64_t fast_distance = 0;
        std::uint32_t fast_ctrl = 0;
        std::uint32_t fast_level = 0;
        bool fast_literal = false;
        bool fast_started = false;
        bool fast_need_ctrl = false;

        state(const compression_kind selected, const std::uint64_t physical, const std::uint64_t logical,
              std::function<void(std::uint64_t, std::span<std::byte>)> reader)
            : kind(selected), compressed_size(physical), logical_size(logical), source(std::move(reader))
        {
            initialize();
        }
        ~state()
        {
            release();
        }
        void release() noexcept
        {
            if (z_ready) {
                static_cast<void>(inflateEnd(&z));
                z_ready = false;
            }
            if (zstd != nullptr) {
                static_cast<void>(ZSTD_freeDStream(zstd));
                zstd = nullptr;
            }
            if (brotli != nullptr) {
                BrotliDecoderDestroyInstance(brotli);
                brotli = nullptr;
            }
        }
        void initialize()
        {
            switch (kind) {
            case compression_kind::fastlz:
                break;
            case compression_kind::deflate:
            case compression_kind::gzip:
                z = {};
                if (inflateInit2(&z, kind == compression_kind::gzip ? 31 : 15) != Z_OK)
                    throw compression_error("failed to initialize streaming zlib decoder");
                z_ready = true;
                break;
            case compression_kind::zstd: {
                std::unique_ptr<ZSTD_DStream, decltype(&ZSTD_freeDStream)> pending(ZSTD_createDStream(),
                                                                                   ZSTD_freeDStream);
                if (pending == nullptr)
                    throw compression_error("failed to create streaming zstd decoder");
                static_cast<void>(require_zstd_result(ZSTD_initDStream(pending.get()),
                                                      "failed to initialize streaming zstd decoder"));
                zstd = pending.release();
                break;
            }
            case compression_kind::brotli:
                brotli = BrotliDecoderCreateInstance(nullptr, nullptr, nullptr);
                if (brotli == nullptr)
                    throw compression_error("failed to initialize streaming brotli decoder");
                break;
            default:
                throw compression_error("compression mode has no streaming decoder");
            }
        }
        void restart()
        {
            release();
            input_count = input_pos = 0;
            physical_read = decoded = window_start = 0;
            window_size = 0;
            complete = false;
            fast_pending = fast_distance = fast_ctrl = fast_level = 0;
            fast_literal = fast_started = fast_need_ctrl = false;
            initialize();
        }
        void refill()
        {
            if (input_pos != input_count || physical_read == compressed_size)
                return;
            input_count = static_cast<std::size_t>(
                (std::min)(compressed_size - physical_read, static_cast<std::uint64_t>(input.size())));
            source(physical_read, std::span<std::byte>(input.data(), input_count));
            physical_read += input_count;
            input_pos = 0;
        }
        [[nodiscard]] std::uint64_t consumed_input() const noexcept
        {
            return physical_read - (input_count - input_pos);
        }
        [[nodiscard]] std::uint8_t fast_byte()
        {
            refill();
            if (input_pos == input_count)
                throw compression_error("fastlz stream is truncated");
            return std::to_integer<std::uint8_t>(input[input_pos++]);
        }
        [[nodiscard]] std::uint64_t fast_output_size() const noexcept
        {
            return (std::max)(logical_size, std::uint64_t{16});
        }
        void fast_emit(const std::uint8_t value)
        {
            // Every token is checked against fast_output_size() before it becomes pending.
            const std::byte byte = static_cast<std::byte>(value);
            output[window_size++] = byte;
            fast_history[static_cast<std::size_t>(decoded % fast_history.size())] = byte;
            ++decoded;
        }
        void fill_fastlz()
        {
            // read() calls fill only while it still needs an in-range logical byte; finish()
            // stops once complete. A completed stream therefore cannot enter this method.
            window_start = decoded;
            window_size = 0;
            if (!fast_started) {
                if (compressed_size < 2)
                    throw compression_error("fastlz stream is too short");
                const std::uint8_t first = fast_byte();
                fast_level = (first >> 5U) + 1U;
                if (fast_level != 1U && fast_level != 2U)
                    throw compression_error("fastlz stream has an unknown level");
                fast_ctrl = first & 31U;
                fast_started = true;
            }
            while (window_size < output.size()) {
                if (fast_pending != 0) {
                    if (fast_literal) {
                        fast_emit(fast_byte());
                    } else {
                        const std::uint64_t origin = decoded - fast_distance;
                        fast_emit(std::to_integer<std::uint8_t>(
                            fast_history[static_cast<std::size_t>(origin % fast_history.size())]));
                    }
                    --fast_pending;
                    continue;
                }
                if (fast_need_ctrl) {
                    const std::uint64_t used = consumed_input();
                    const bool ended = fast_level == 1U ? used > compressed_size - 2 : used >= compressed_size;
                    if (ended) {
                        if (used != compressed_size || decoded != fast_output_size())
                            throw compression_error("fastlz block length does not match its envelope");
                        complete = true;
                        break;
                    }
                    fast_ctrl = fast_byte();
                    fast_need_ctrl = false;
                }
                if (fast_ctrl < 32U) {
                    fast_literal = true;
                    fast_pending = fast_ctrl + 1U;
                } else {
                    fast_literal = false;
                    std::uint64_t len = (fast_ctrl >> 5U) - 1U;
                    const std::uint32_t ofs = (fast_ctrl & 31U) << 8U;
                    if (len == 6U) {
                        if (fast_level == 1U) {
                            len += fast_byte();
                        } else {
                            std::uint8_t extension = 0;
                            do {
                                extension = fast_byte();
                                len += extension;
                                if (len > fast_output_size() - decoded)
                                    throw compression_error("fastlz match exceeds padded size");
                            } while (extension == 255U);
                        }
                    }
                    const std::uint8_t code = fast_byte();
                    fast_distance = static_cast<std::uint64_t>(ofs) + code + 1U;
                    if (fast_level == 2U && code == 255U && ofs == (31U << 8U)) {
                        const std::uint32_t high = fast_byte();
                        const std::uint32_t low = fast_byte();
                        const std::uint32_t far = high << 8U | low;
                        fast_distance = static_cast<std::uint64_t>(far) + 8191U + 1U;
                    }
                    fast_pending = len + 3U;
                    if (fast_distance > decoded)
                        throw compression_error("fastlz match distance is outside history");
                }
                if (fast_pending > fast_output_size() - decoded)
                    throw compression_error("fastlz token exceeds padded size");
                fast_need_ctrl = true;
            }
        }
        void fill()
        {
            if (kind == compression_kind::fastlz) {
                fill_fastlz();
                return;
            }
            window_size = 0;
            while (window_size == 0 && !complete) {
                refill();
                const std::size_t available = input_count - input_pos;
                std::size_t consumed = 0;
                std::size_t produced = 0;
                bool ended = false;
                if (z_ready) {
                    z.next_in = reinterpret_cast<Bytef *>(input.data() + input_pos);
                    z.avail_in = static_cast<uInt>(available);
                    z.next_out = reinterpret_cast<Bytef *>(output.data());
                    z.avail_out = static_cast<uInt>(output.size());
                    const int result = inflate(&z, Z_NO_FLUSH);
                    consumed = available - z.avail_in;
                    produced = output.size() - z.avail_out;
                    ended = result == Z_STREAM_END;
                    if (!ended && result != Z_OK)
                        throw compression_error("streaming zlib block is malformed");
                } else if (zstd != nullptr) {
                    ZSTD_inBuffer in{input.data() + input_pos, available, 0};
                    ZSTD_outBuffer out{output.data(), output.size(), 0};
                    const std::size_t result = ZSTD_decompressStream(zstd, &out, &in);
                    static_cast<void>(require_zstd_result(result, "streaming zstd block is malformed"));
                    consumed = in.pos;
                    produced = out.pos;
                    ended = result == 0;
                } else {
                    const std::uint8_t *next_in = reinterpret_cast<const std::uint8_t *>(input.data() + input_pos);
                    std::size_t avail_in = available;
                    std::uint8_t *next_out = reinterpret_cast<std::uint8_t *>(output.data());
                    std::size_t avail_out = output.size();
                    std::size_t total_out = 0;
                    const BrotliDecoderResult result =
                        BrotliDecoderDecompressStream(brotli, &avail_in, &next_in, &avail_out, &next_out, &total_out);
                    if (result == BROTLI_DECODER_RESULT_ERROR)
                        throw compression_error("streaming brotli block is malformed");
                    consumed = available - avail_in;
                    produced = output.size() - avail_out;
                    ended = result == BROTLI_DECODER_RESULT_SUCCESS;
                }
                input_pos += consumed;
                if (produced > logical_size - decoded)
                    throw compression_error("streaming block expands beyond declared size");
                window_start = decoded;
                window_size = produced;
                decoded += produced;
                if (ended) {
                    const std::uint64_t consumed_total = physical_read - (input_count - input_pos);
                    if (consumed_total != compressed_size || decoded != logical_size)
                        throw compression_error("streaming block length does not match its envelope");
                    complete = true;
                } else if (consumed == 0 && produced == 0) {
                    // With unconsumed input the codec must consume or produce; once input is
                    // exhausted, refill cannot advance and another call would repeat forever.
                    throw compression_error("streaming block is truncated");
                }
            }
        }
        void finish()
        {
            while (!complete)
                fill();
        }
        void read(const std::uint64_t offset, const std::span<std::byte> destination)
        {
            if (offset > logical_size || destination.size() > logical_size - offset)
                throw compression_error("streaming block read exceeds logical size");
            if (destination.empty())
                return;
            if (offset < window_start)
                restart();
            std::size_t copied = 0;
            while (copied < destination.size()) {
                const std::uint64_t position = offset + copied;
                if (position >= window_start + window_size) {
                    fill();
                    continue;
                }
                const std::size_t within = static_cast<std::size_t>(position - window_start);
                const std::size_t amount = (std::min)(destination.size() - copied, window_size - within);
                std::memcpy(destination.data() + copied, output.data() + within, amount);
                copied += amount;
            }
            if (offset + destination.size() == logical_size && !complete) {
                fill();
                // fill() rejects any output beyond logical_size before updating the window.
            }
        }
    };

    block_stream_decoder::block_stream_decoder(const compression_kind kind, const std::uint64_t compressed_size,
                                               const std::uint64_t logical_size,
                                               std::function<void(std::uint64_t, std::span<std::byte>)> source)
        : state_(std::make_unique<state>(kind, compressed_size, logical_size, std::move(source)))
    {
    }
    block_stream_decoder::~block_stream_decoder() = default;
    void block_stream_decoder::read(const std::uint64_t offset, const std::span<std::byte> destination)
    {
        state_->read(offset, destination);
    }
    void block_stream_decoder::finish()
    {
        state_->finish();
    }
} // namespace unpackrat::codecs
