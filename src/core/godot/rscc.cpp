#include "rscc.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <utility>
#include <vector>

namespace unpackrat::godot
{
    namespace
    {
        constexpr std::array<std::byte, 4> rscc_magic{std::byte{0x52}, std::byte{0x53}, std::byte{0x43},
                                                      std::byte{0x43}}; // "RSCC"
        constexpr std::size_t rscc_header_bytes = 16;
        constexpr std::size_t rscc_table_entry_bytes = 4;
        constexpr std::size_t rscc_table_batch_bytes = 4096;

        [[nodiscard]] std::uint32_t read_u32_le(const std::span<const std::byte> input,
                                                const std::size_t offset) noexcept
        {
            const auto at = [&input](const std::size_t index) {
                return static_cast<std::uint32_t>(std::to_integer<std::uint8_t>(input[index]));
            };
            return at(offset) | (at(offset + 1) << 8U) | (at(offset + 2) << 16U) | (at(offset + 3) << 24U);
        }

        // The fixed header and envelope fields a caller needs before it commits to
        // decompression. `data_start` is the first compressed block byte and `data_end`
        // the trailing magic, so both the size inspection and the full decoder share one
        // wire parse instead of duplicating the header grammar.
        struct rscc_header final
        {
            codecs::compression_kind kind;
            std::uint64_t block_size;
            std::uint64_t total;
            std::uint64_t block_count;
            std::uint64_t data_start;
            std::uint64_t data_end;
        };

        // Validates the fixed header and envelope and throws `rscc_error` on any defect,
        // without reading or allocating a single compressed byte. The checks run in the
        // same order the decoder has always enforced them.
        [[nodiscard]] rscc_header parse_rscc_header(const std::span<const std::byte> input, const rscc_limits &limits)
        {
            const auto input_bytes = static_cast<std::uint64_t>(input.size());
            if (input_bytes > limits.max_input_bytes) {
                throw rscc_error("rscc stream exceeds the configured input budget");
            }
            if (input_bytes < rscc_header_bytes) {
                throw rscc_error("rscc stream is shorter than the header");
            }
            if (!std::equal(rscc_magic.begin(), rscc_magic.end(), input.begin())) {
                throw rscc_error("rscc header magic is missing");
            }
            if (!std::equal(rscc_magic.begin(), rscc_magic.end(), input.end() - rscc_magic.size())) {
                throw rscc_error("rscc trailing magic is missing");
            }

            const std::uint32_t mode = read_u32_le(input, 4);
            const std::uint64_t block_size = read_u32_le(input, 8);
            const std::uint64_t total = read_u32_le(input, 12);
            if (block_size == 0) {
                throw rscc_error("rscc block size is zero");
            }
            if (mode > static_cast<std::uint32_t>(codecs::compression_kind::brotli)) {
                throw rscc_error("rscc compression mode is not supported");
            }
            if (total > limits.max_output_bytes) {
                throw rscc_error("rscc logical size exceeds the configured output budget");
            }

            const std::uint64_t block_count = total / block_size + 1;
            if (block_count > limits.max_blocks) {
                throw rscc_error("rscc block count exceeds the configured limit");
            }

            const std::uint64_t table_bytes = block_count * rscc_table_entry_bytes;
            const std::uint64_t data_start = rscc_header_bytes + table_bytes;
            const std::uint64_t data_end = input_bytes - rscc_magic.size();
            if (data_start > data_end) {
                throw rscc_error("rscc block table runs past the stored data");
            }

            return rscc_header{
                static_cast<codecs::compression_kind>(mode), block_size, total, block_count, data_start, data_end};
        }
    } // namespace

    std::vector<std::byte> decompress_rscc(const std::span<const std::byte> input, const rscc_limits &limits)
    {
        const rscc_header header = parse_rscc_header(input, limits);
        // A caller's uint64_t budget may exceed the codec's size_t bound on Win32.
        const auto codec_output_limit = static_cast<std::size_t>(
            (std::min)(limits.max_output_bytes, static_cast<std::uint64_t>((std::numeric_limits<std::size_t>::max)())));
        std::vector<std::byte> output;
        output.reserve(static_cast<std::size_t>(header.total));
        std::uint64_t offset = header.data_start;
        for (std::uint64_t index = 0; index < header.block_count; ++index) {
            const auto compressed_size =
                read_u32_le(input, static_cast<std::size_t>(rscc_header_bytes + index * rscc_table_entry_bytes));
            if (compressed_size > limits.max_block_bytes) {
                throw rscc_error("rscc block size exceeds the configured limit");
            }
            const std::uint64_t next_offset = offset + compressed_size;
            if (next_offset > header.data_end) {
                throw rscc_error("rscc block runs past the stored data");
            }

            const std::uint64_t logical_size =
                (index + 1 == header.block_count) ? header.total % header.block_size : header.block_size;
            // Translate only the codec's own error type; allocation and other failures
            // must propagate unchanged rather than being masked as rscc_error.
            std::vector<std::byte> block;
            try {
                block =
                    codecs::decompress_block(input.subspan(static_cast<std::size_t>(offset), compressed_size),
                                             header.kind, static_cast<std::size_t>(logical_size), codec_output_limit);
            } catch (const codecs::compression_error &) {
                throw rscc_error("rscc block failed to decompress");
            }
            output.insert(output.end(), block.begin(), block.end());
            offset = next_offset;
        }
        if (offset != header.data_end) {
            throw rscc_error("rscc stored data does not match the block table");
        }
        return output;
    }

    std::uint64_t rscc_logical_size(const std::span<const std::byte> input, const rscc_limits &limits)
    {
        return parse_rscc_header(input, limits).total;
    }

    rscc_metadata_reader::rscc_metadata_reader(const std::uint64_t stored_size,
                                               std::function<void(std::uint64_t, std::span<std::byte>)> read_stored,
                                               const rscc_limits &limits)
        : read_stored_(std::move(read_stored)), limits_(limits)
    {
        if (stored_size > limits.max_input_bytes || stored_size < rscc_header_bytes + rscc_magic.size()) {
            throw rscc_error("rscc metadata envelope is outside the input budget");
        }
        std::array<std::byte, rscc_header_bytes> header{};
        std::array<std::byte, 4> trailer{};
        read_stored_(0, header);
        read_stored_(stored_size - trailer.size(), trailer);
        if (!std::equal(rscc_magic.begin(), rscc_magic.end(), header.begin()) || trailer != rscc_magic) {
            throw rscc_error("rscc metadata magic is missing");
        }
        const std::uint32_t mode = read_u32_le(header, 4);
        block_size_ = read_u32_le(header, 8);
        total_ = read_u32_le(header, 12);
        if (block_size_ == 0 || mode > static_cast<std::uint32_t>(codecs::compression_kind::brotli) ||
            total_ > limits.max_output_bytes) {
            throw rscc_error("rscc metadata header is unsupported");
        }
        kind_ = static_cast<codecs::compression_kind>(mode);
        block_count_ = total_ / block_size_ + 1;
        if (block_count_ > limits.max_blocks ||
            block_count_ > (stored_size - rscc_header_bytes - rscc_magic.size()) / rscc_table_entry_bytes) {
            throw rscc_error("rscc metadata table is outside the input");
        }
        data_start_ = rscc_header_bytes + block_count_ * rscc_table_entry_bytes;
        data_end_ = stored_size - rscc_magic.size();
        // The table is metadata, so account for every compressed block before exposing a
        // logical view. This rejects hidden bytes before the trailer without decoding media.
        std::array<std::byte, rscc_table_batch_bytes> entries{};
        std::uint64_t accounted = data_start_;
        for (std::uint64_t index = 0; index < block_count_;) {
            const std::uint64_t batch =
                (std::min)(block_count_ - index, static_cast<std::uint64_t>(entries.size() / rscc_table_entry_bytes));
            const auto bytes =
                std::span<std::byte>(entries.data(), static_cast<std::size_t>(batch * rscc_table_entry_bytes));
            read_stored_(rscc_header_bytes + index * rscc_table_entry_bytes, bytes);
            for (std::uint64_t item = 0; item < batch; ++item) {
                const std::uint32_t compressed =
                    read_u32_le(bytes, static_cast<std::size_t>(item * rscc_table_entry_bytes));
                if (compressed > data_end_ - accounted) {
                    throw rscc_error("rscc metadata block table exceeds the source");
                }
                accounted += compressed;
            }
            index += batch;
        }
        if (accounted != data_end_) {
            throw rscc_error("rscc metadata has trailing bytes outside its block table");
        }
        next_offset_ = data_start_;
    }

    void rscc_metadata_reader::load(const std::uint64_t index)
    {
        if (index == cached_index_) {
            return;
        }
        if (index < next_index_) {
            next_index_ = 0;
            next_offset_ = data_start_;
        }
        std::array<std::byte, rscc_table_batch_bytes> entries{};
        while (next_index_ <= index) {
            const std::uint64_t batch = (std::min)(index - next_index_ + 1,
                                                   static_cast<std::uint64_t>(entries.size() / rscc_table_entry_bytes));
            const auto bytes =
                std::span<std::byte>(entries.data(), static_cast<std::size_t>(batch * rscc_table_entry_bytes));
            read_stored_(rscc_header_bytes + next_index_ * rscc_table_entry_bytes, bytes);
            for (std::uint64_t item = 0; item < batch; ++item) {
                const std::uint32_t compressed_size =
                    read_u32_le(bytes, static_cast<std::size_t>(item * rscc_table_entry_bytes));
                if (compressed_size > data_end_ - next_offset_) {
                    throw rscc_error("rscc metadata block exceeds the source");
                }
                if (next_index_ == index) {
                    const std::uint64_t logical_bytes = index + 1 == block_count_ ? total_ % block_size_ : block_size_;
                    if (logical_bytes > limits_.max_block_bytes || compressed_size > limits_.max_block_bytes) {
                        throw rscc_error("rscc metadata block exceeds the configured limit");
                    }
                    const std::uint64_t block_start = next_offset_;
                    try {
                        streamed_block_ = std::make_unique<codecs::block_stream_decoder>(
                            kind_, compressed_size, logical_bytes,
                            [this, block_start](const std::uint64_t offset, const std::span<std::byte> target) {
                                read_stored_(block_start + offset, target);
                            });
                    } catch (const codecs::compression_error &) {
                        throw rscc_error("rscc metadata block has no streaming decoder");
                    }
                    cached_index_ = index;
                }
                next_offset_ += compressed_size;
                ++next_index_;
            }
        }
    }

    void rscc_metadata_reader::read(const std::uint64_t offset, const std::span<std::byte> destination)
    {
        if (offset > total_ || destination.size() > total_ - offset) {
            throw rscc_error("rscc metadata read is outside the logical stream");
        }
        std::size_t copied = 0;
        while (copied < destination.size()) {
            const std::uint64_t logical = offset + copied;
            const std::uint64_t index = logical / block_size_;
            const std::size_t within = static_cast<std::size_t>(logical % block_size_);
            load(index);
            const std::size_t count = static_cast<std::size_t>(
                (std::min)(static_cast<std::uint64_t>(destination.size() - copied),
                           (index + 1 == block_count_ ? total_ % block_size_ : block_size_) - within));
            try {
                streamed_block_->read(within, destination.subspan(copied, count));
            } catch (const codecs::compression_error &) {
                throw rscc_error("rscc metadata block failed to decompress");
            }
            copied += count;
        }
    }
    void rscc_metadata_reader::validate_terminal_block()
    {
        if (total_ % block_size_ != 0) {
            return;
        }
        load(block_count_ - 1);
        try {
            streamed_block_->finish();
        } catch (const codecs::compression_error &) {
            throw rscc_error("rscc terminal block failed to decompress");
        }
    }
} // namespace unpackrat::godot
