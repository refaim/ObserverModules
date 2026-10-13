#include "qoa_codec.h"

#include "audio_preflight.h"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <limits>

// The pinned QOA overlay builds Godot's reference implementation as a C static library. Its types
// stay private to this adapter, which uses caller-owned buffers for frame decoding.
//   godot @ 876b290332ec6f2e6d173d08162a02aa7e6ca46d, thirdparty/misc/qoa.h
//   sha256 ab501e91f4afd18c811e689713e5fd3956030562bf80cda784d304af17133f01
#pragma warning(push, 0)
#include <qoa.h>
#pragma warning(pop)

namespace unpackrat::codecs
{
    namespace
    {
        constexpr std::size_t qoa_header_bytes = 8;
        constexpr std::uint32_t qoa_max_phase_channels = 2;

        struct qoa_frame_header
        {
            std::uint32_t channels;
            std::uint32_t samplerate;
            std::uint32_t samples;
            std::uint32_t frame_size;
        };

        // A QOA frame header is eight big-endian bytes: channels(1) samplerate(3) samples(2) size(2).
        qoa_frame_header parse_frame_header(const std::byte *const bytes) noexcept
        {
            std::uint64_t packed = 0;
            for (std::size_t index = 0; index < 8; ++index) {
                packed = (packed << 8) | std::to_integer<std::uint64_t>(bytes[index]);
            }
            return qoa_frame_header{
                static_cast<std::uint32_t>((packed >> 56) & 0xffU),
                static_cast<std::uint32_t>((packed >> 32) & 0xffffffU),
                static_cast<std::uint32_t>((packed >> 16) & 0xffffU),
                static_cast<std::uint32_t>(packed & 0xffffU),
            };
        }
    } // namespace

    pcm_audio decode_qoa(const std::span<const std::byte> input, const audio_limits &limits)
    {
        // qoa_decode_header takes an int byte count, so the effective input cap is the smaller of
        // the caller's budget and the reference decoder's representable range.
        const std::size_t size_limit =
            std::min(limits.max_input_bytes, static_cast<std::size_t>(std::numeric_limits<int>::max()));
        if (input.size() > size_limit) {
            throw audio_error("qoa: input exceeds the configured input byte limit");
        }

        qoa_desc desc{};
        if (qoa_decode_header(reinterpret_cast<const unsigned char *>(input.data()), static_cast<int>(input.size()),
                              &desc) != qoa_header_bytes) {
            throw audio_error("qoa: the stream header is missing, malformed, or truncated");
        }
        // A successful header always reports at least one channel; the Godot phase only carries one
        // or two, so any wider stream is rejected before it drives the frame loop.
        if (desc.channels > qoa_max_phase_channels) {
            throw audio_error("qoa: only mono and stereo streams are supported");
        }

        const std::size_t channels = desc.channels;
        // The file header's frame count is untrusted, so the frame budget is enforced before the
        // count is multiplied by the channel count and before anything is allocated.
        if (desc.samples > limits.max_pcm_frames) {
            throw audio_error("qoa: declared frame count exceeds the configured limit");
        }
        const std::size_t total_samples = preflight::interleaved_sample_count(desc.samples, channels);
        if (total_samples > limits.max_output_bytes / sizeof(std::int16_t)) {
            throw audio_error("qoa: decoded size exceeds the configured output byte limit");
        }

        pcm_audio result;
        result.sample_rate = desc.samplerate;
        result.channels = desc.channels;
        result.samples.resize(total_samples);

        std::size_t offset = qoa_header_bytes;
        std::size_t decoded = 0;
        while (decoded < desc.samples) {
            if (input.size() - offset < qoa_header_bytes) {
                throw audio_error("qoa: a frame header is truncated");
            }
            const qoa_frame_header header = parse_frame_header(input.data() + offset);
            if (header.channels != desc.channels) {
                throw audio_error("qoa: a frame disagrees with the file channel count");
            }
            if (header.samplerate != desc.samplerate) {
                throw audio_error("qoa: a frame disagrees with the file sample rate");
            }
            // The reference decoder reads one 8-byte slice per channel for every 20 samples and
            // never checks that the frame actually funds them, so a frame that declares more
            // samples than it carries makes it read past the end of the stream. The declared count
            // must be a whole frame's worth, and the frame's own size field must be exactly the
            // per-format size for that many slices while staying inside the remaining input.
            if (header.samples == 0 || header.samples > QOA_FRAME_LEN || decoded + header.samples > desc.samples) {
                throw audio_error("qoa: a frame declares an out-of-range sample count");
            }
            const std::size_t slices = (header.samples + QOA_SLICE_LEN - 1) / QOA_SLICE_LEN;
            const std::size_t required_frame_size = qoa_header_bytes + 16 * channels + slices * 8 * channels;
            if (header.frame_size != required_frame_size || header.frame_size > input.size() - offset) {
                throw audio_error("qoa: a frame size is out of range");
            }

            unsigned int frame_len = 0;
            const unsigned int consumed =
                qoa_decode_frame(reinterpret_cast<const unsigned char *>(input.data()) + offset, header.frame_size,
                                 &desc, result.samples.data() + decoded * channels, &frame_len);
            // The validation above mirrors the reference decoder's acceptance exactly, so every
            // frame decodes the count it declared; advancing by the declared count and the bytes
            // the decoder reports keeps the walk faithful and guarantees forward progress.
            offset += consumed;
            decoded += header.samples;
        }
        return result;
    }

    std::uint32_t decode_qoa_frame(const std::span<const std::byte> frame, const std::uint32_t channels,
                                   const std::uint32_t sample_rate, const std::span<std::int16_t> output)
    {
        if (channels == 0 || channels > qoa_max_phase_channels || sample_rate == 0 || frame.size() < qoa_header_bytes) {
            throw audio_error("qoa: a streaming frame has invalid geometry");
        }
        const qoa_frame_header header = parse_frame_header(frame.data());
        if (header.channels != channels || header.samplerate != sample_rate || header.samples == 0 ||
            header.samples > QOA_FRAME_LEN || output.size() < static_cast<std::size_t>(header.samples) * channels) {
            throw audio_error("qoa: a streaming frame header disagrees with its descriptor");
        }
        const std::size_t slices = (header.samples + QOA_SLICE_LEN - 1) / QOA_SLICE_LEN;
        const std::size_t expected = qoa_header_bytes + std::size_t{16} * channels + slices * std::size_t{8} * channels;
        if (header.frame_size != expected || frame.size() != expected) {
            throw audio_error("qoa: a streaming frame has invalid length");
        }
        qoa_desc desc{};
        desc.channels = channels;
        desc.samplerate = sample_rate;
        unsigned int frame_len = 0;
        // The preflight exactly mirrors the pinned decoder's acceptance: matching channels/rate,
        // complete LMS state and ceil(samples/20) slices per channel. It therefore consumes this
        // exact frame and assigns frame_len=header.samples without any remaining failure case.
        static_cast<void>(qoa_decode_frame(reinterpret_cast<const unsigned char *>(frame.data()),
                                           static_cast<unsigned int>(frame.size()), &desc, output.data(), &frame_len));
        return header.samples;
    }
} // namespace unpackrat::codecs
