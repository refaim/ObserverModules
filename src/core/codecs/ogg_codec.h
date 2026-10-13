#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <limits>
#include <span>
#include <stdexcept>

namespace unpackrat::codecs
{
    // A single logical bitstream packet offered to the muxer. `data` is borrowed
    // for the duration of the write_ogg call only. A granule position of -1 marks
    // an unknown position; any other value must be non-negative.
    struct ogg_packet_view
    {
        std::span<const std::byte> data;
        std::int64_t granule_position = -1;
    };

    struct ogg_packet_location final
    {
        std::uint64_t length = 0;
        std::int64_t granule_position = -1;
    };

    // Optional transport budgets. Their defaults impose no arbitrary stream or packet ceiling;
    // each actual packet is still checked against libogg's signed-long address range.
    struct ogg_limits
    {
        std::size_t max_input_bytes = (std::numeric_limits<std::size_t>::max)();
        std::size_t max_output_bytes = (std::numeric_limits<std::size_t>::max)();
        std::size_t max_packets = (std::numeric_limits<std::size_t>::max)();
    };

    class ogg_error final : public std::runtime_error
    {
      public:
        using std::runtime_error::runtime_error;
    };

    // Validate a logical packet length before the transport's signed-length conversion.
    // This scalar preflight also applies when packet bytes are streamed from a source.
    void check_ogg_packet_size(std::uint64_t bytes);

    // Muxes one logical Ogg bitstream. The produced bytes are delivered, in order,
    // to `sink` as borrowed views that are only valid during the call. The first
    // packet opens a beginning-of-stream page and the last packet closes the
    // stream. This is transport framing only: packet contents (for example Vorbis
    // headers) are never inspected or validated here.
    //
    // The first packet may only carry an unspecified (-1, normalized to the BOS
    // value 0) or zero granule position; a positive first value is rejected before
    // the framing library or the sink is reached. Every later known granule
    // position must be non-negative and non-decreasing, and each one survives the
    // round trip exactly; unspecified (-1) packets may share a page.
    void write_ogg(std::span<const ogg_packet_view> packets,
                   const std::function<void(std::span<const std::byte>)> &sink, const ogg_limits &limits = {});

    // Frame pages directly from packet ranges so even one large packet uses bounded storage.
    void write_ogg_paged(std::span<const ogg_packet_location> packets,
                         const std::function<void(std::size_t, std::uint64_t, std::span<std::byte>)> &fetch,
                         const std::function<void(std::span<const std::byte>)> &sink, const ogg_limits &limits = {});
} // namespace unpackrat::codecs
