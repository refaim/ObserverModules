// dr_mp3 is compiled into exactly this translation unit; its headers, types and error codes stay
// private to the adapter and never appear in our public API. The adapter drives the decoder with
// caller-owned output buffers, so it never hands out a dr_mp3-allocated buffer.
#define DR_MP3_IMPLEMENTATION
#pragma warning(push, 0)
#include <../third_party/dr_libs/dr_mp3.h>
#pragma warning(pop)

#include "mp3_codec.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <vector>

namespace unpackrat::codecs
{
    namespace
    {
        // MPEG-1 Layer III frames carry 1152 PCM frames each (MPEG-2 carries 576). Asking the
        // decoder for one MPEG-1 frame at a time bounds the working buffer regardless of version.
        constexpr std::size_t mp3_frames_per_step = 1152;

        // The ID3v2 header is ten bytes; a footer flag adds a second ten-byte block.
        constexpr std::size_t id3_header_bytes = 10;
        constexpr std::size_t id3_footer_bytes = 10;
        // dr_mp3 checks at most ten sync positions, and a free-format frame is at most 2304
        // bytes in this pinned decoder. Two extra frames fund free-format size discovery.
        constexpr std::size_t first_frame_window =
            DRMP3_MAX_FREE_FORMAT_FRAME_SIZE * (DRMP3_MAX_FRAME_SYNC_MATCHES + 2) + DRMP3_HDR_SIZE;

        [[nodiscard]] bool has_id3_magic(const std::span<const std::byte> input) noexcept
        {
            const unsigned tag = (std::to_integer<unsigned>(input[0]) << 16) |
                                 (std::to_integer<unsigned>(input[1]) << 8) | std::to_integer<unsigned>(input[2]);
            return tag == 0x494433U; // "ID3"
        }

        [[nodiscard]] std::uint64_t id3_frame_offset(const std::span<const std::byte> prefix,
                                                     const std::uint64_t length)
        {
            if (prefix.size() < id3_header_bytes || !has_id3_magic(prefix)) {
                return 0;
            }
            std::uint32_t tag_size = 0;
            for (std::size_t index = 6; index < 10; ++index) {
                const unsigned part = std::to_integer<unsigned>(prefix[index]);
                if ((part & 0x80U) != 0) {
                    throw audio_error("mp3: an ID3 size byte has the high bit set");
                }
                tag_size = (tag_size << 7) | (part & 0x7FU);
            }
            std::uint64_t offset = id3_header_bytes + static_cast<std::uint64_t>(tag_size);
            if ((std::to_integer<unsigned>(prefix[5]) & 0x10U) != 0) {
                offset += id3_footer_bytes;
            }
            if (offset > length) {
                throw audio_error("mp3: an ID3v2 tag extends past the input");
            }
            return offset;
        }

        // The pure MPEG frame size the decoder measured for the header it selected, reconstructed
        // from the metadata it already reports (the sample rate and bitrate it read) plus the two
        // header bits it interpreted for the per-frame sample count and the padding slot. Compared
        // against the number of bytes the decoder says it consumed, a difference is exactly the
        // count of junk bytes it had to skip before reaching the frame, which the pass-through
        // contract (an optional ID3v2 tag immediately followed by one MPEG frame) forbids.
        [[nodiscard]] std::size_t measured_frame_bytes(const drmp3dec &decoder,
                                                       const drmp3dec_frame_info &info) noexcept
        {
            const drmp3_uint8 *const header = decoder.header;
            // Only Layer III reaches this check, so the frame always carries 1152 PCM frames for
            // MPEG-1 and 576 for MPEG-2/2.5; the header's MPEG-1 flag selects between them.
            const std::uint32_t samples = (header[1] & 0x08U) != 0 ? 1152U : 576U;
            std::uint32_t bytes = samples * static_cast<std::uint32_t>(info.bitrate_kbps) * 125U /
                                  static_cast<std::uint32_t>(info.sample_rate);
            // A free-format frame declares bitrate index 0, so it carries no bitrate field and the
            // size computed above is zero. The decoder learns the frame size from the sync-word
            // spacing and records it in ``free_format_bytes`` (dr_mp3.h drmp3_hdr_frame_bytes), so
            // reproduce that learned size instead of folding in the zero bitrate.
            if (bytes == 0U) {
                bytes = static_cast<std::uint32_t>(decoder.free_format_bytes);
            }
            if ((header[2] & 0x02U) != 0) {
                bytes += 1U;
            }
            return bytes;
        }

        // Owns a dr_mp3 decoder and everything the library allocates for it. The matching
        // ``drmp3_uninit`` runs on every exit path, including exceptions. A failed init leaves
        // nothing to release, so the guard only arms once initialization has succeeded.
        class decoder_guard
        {
          public:
            decoder_guard() = default;
            decoder_guard(const decoder_guard &) = delete;
            decoder_guard &operator=(const decoder_guard &) = delete;

            ~decoder_guard()
            {
                if (armed_) {
                    drmp3_uninit(decoder_.get());
                }
            }

            [[nodiscard]] drmp3 *decoder() noexcept
            {
                return decoder_.get();
            }
            void arm() noexcept
            {
                armed_ = true;
            }

          private:
            std::unique_ptr<drmp3> decoder_ = std::make_unique<drmp3>();
            bool armed_ = false;
        };

        [[nodiscard]] mp3_stream_info probe_first_frame(const std::span<const std::byte> available)
        {
            const std::span<const std::byte> frames = available.first((std::min)(available.size(), first_frame_window));
            if (frames.empty()) {
                throw audio_error("mp3: no complete MPEG frame is present");
            }
            auto decoder = std::make_unique<drmp3dec>();
            drmp3dec_init(decoder.get());
            drmp3dec_frame_info info{};
            const int decoded =
                drmp3dec_decode_frame(decoder.get(), reinterpret_cast<const drmp3_uint8 *>(frames.data()),
                                      static_cast<int>(frames.size()), nullptr, &info);
            if (decoded == 0) {
                throw audio_error("mp3: no complete MPEG frame is present");
            }
            if (info.layer != 3) {
                throw audio_error("mp3: only MPEG Layer III streams are supported");
            }
            if (static_cast<std::size_t>(info.frame_bytes) != measured_frame_bytes(*decoder, info)) {
                throw audio_error("mp3: the first MPEG frame does not begin at the start of the payload");
            }
            mp3_stream_info result;
            result.sample_rate = static_cast<std::uint32_t>(info.sample_rate);
            result.channels = static_cast<std::uint32_t>(info.channels);
            result.frame_bytes = static_cast<std::size_t>(info.frame_bytes);
            return result;
        }
    } // namespace

    mp3_stream_info probe_mp3(const std::span<const std::byte> input, const audio_limits &limits)
    {
        if (input.empty()) {
            throw audio_error("mp3: input is empty");
        }
        if (input.size() > limits.max_input_bytes) {
            throw audio_error("mp3: input exceeds the configured byte budget");
        }
        const std::uint64_t offset =
            id3_frame_offset(input.first((std::min)(input.size(), id3_header_bytes)), input.size());
        return probe_first_frame(input.subspan(static_cast<std::size_t>(offset)));
    }

    mp3_stream_info probe_mp3_source(const io::byte_source &source, const audio_limits &limits)
    {
        if (source.size == 0) {
            throw audio_error("mp3: input is empty");
        }
        if (source.size > limits.max_input_bytes) {
            throw audio_error("mp3: input exceeds the configured byte budget");
        }
        std::array<std::byte, id3_header_bytes> prefix{};
        const std::size_t prefix_size =
            static_cast<std::size_t>((std::min)(source.size, static_cast<std::uint64_t>(id3_header_bytes)));
        source.read(0, std::span<std::byte>(prefix.data(), prefix_size));
        const std::uint64_t offset =
            id3_frame_offset(std::span<const std::byte>(prefix.data(), prefix_size), source.size);
        auto buffer_storage = std::make_unique<std::array<std::byte, first_frame_window>>();
        auto &buffer = *buffer_storage;
        const std::size_t window =
            static_cast<std::size_t>((std::min)(source.size - offset, static_cast<std::uint64_t>(first_frame_window)));
        if (window != 0) {
            source.read(offset, std::span<std::byte>(buffer.data(), window));
        }
        return probe_first_frame(std::span<const std::byte>(buffer.data(), window));
    }

    pcm_audio decode_mp3(const std::span<const std::byte> input, const audio_limits &limits)
    {
        if (input.empty()) {
            throw audio_error("mp3: input is empty");
        }
        if (input.size() > limits.max_input_bytes) {
            throw audio_error("mp3: input exceeds the configured byte budget");
        }

        decoder_guard decoder;
        if (drmp3_init_memory(decoder.decoder(), input.data(), input.size(), nullptr) != DRMP3_TRUE) {
            throw audio_error("mp3: input is not a decodable MP3 stream");
        }
        decoder.arm();

        // dr_mp3 reports the stream rate and channel count from the first accepted frame and only
        // ever produces one or two channels, exactly the range the Godot phase accepts, so the
        // per-frame byte count below can neither be zero nor exceed the channel budget.
        pcm_audio result;
        result.sample_rate = decoder.decoder()->sampleRate;
        result.channels = decoder.decoder()->channels;

        const std::size_t channels = result.channels;
        const std::size_t bytes_per_frame = channels * 2;
        std::vector<drmp3_int16> step(mp3_frames_per_step * channels);

        std::size_t frames = 0;
        for (;;) {
            const drmp3_uint64 read = drmp3_read_pcm_frames_s16(decoder.decoder(), mp3_frames_per_step, step.data());
            if (read == 0) {
                break;
            }

            const std::size_t incoming = static_cast<std::size_t>(read);
            if (incoming > limits.max_pcm_frames - frames) {
                throw audio_error("mp3: decoded frame count exceeds the configured budget");
            }
            const std::size_t total_frames = frames + incoming;
            if (total_frames > limits.max_output_bytes / bytes_per_frame) {
                throw audio_error("mp3: decoded PCM exceeds the configured byte budget");
            }

            const auto step_end = step.begin() + static_cast<std::ptrdiff_t>(incoming * channels);
            result.samples.insert(result.samples.end(), step.begin(), step_end);
            frames = total_frames;
        }

        return result;
    }
} // namespace unpackrat::codecs
