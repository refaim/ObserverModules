#include "sample_fixtures.h"

#include <algorithm>
#include <cstdint>
#include <stdexcept>

// The pinned QOA overlay provides the reference encoder in its static library. Only declarations
// are needed here, so fixtures and the adapter exercise the same decoder/encoder implementation.
#define QOA_NO_STDIO
#include <qoa.h>

namespace test::support
{
    namespace
    {
        // The four zero bytes Godot's writer prefixes every packed mono lane with.
        constexpr std::size_t ima_seed_bytes = 4;
    } // namespace

    std::vector<std::byte> make_qoa_stream(const std::uint32_t channels, const std::uint32_t sample_rate,
                                           const std::span<const std::int16_t> interleaved)
    {
        if (channels == 0 || channels > QOA_MAX_CHANNELS) {
            throw std::invalid_argument("QOA fixture: channel count out of range");
        }
        if (sample_rate == 0 || sample_rate > 0xffffff) {
            throw std::invalid_argument("QOA fixture: sample rate out of range");
        }
        if (interleaved.size() % channels != 0) {
            throw std::invalid_argument("QOA fixture: interleaved length is not a whole number of frames");
        }

        qoa_desc desc{};
        desc.channels = channels;
        desc.samplerate = sample_rate;
        desc.samples = static_cast<unsigned int>(interleaved.size() / channels);
        if (desc.samples == 0) {
            throw std::invalid_argument("QOA fixture: a stream needs at least one frame");
        }
        for (std::uint32_t channel = 0; channel < channels; ++channel) {
            // Seed the LMS filter the same way Godot's importer does: weights {0, 0, -1, 2}, no history.
            desc.lms[channel].weights[0] = 0;
            desc.lms[channel].weights[1] = 0;
            desc.lms[channel].weights[2] = -(1 << 13);
            desc.lms[channel].weights[3] = (1 << 14);
            for (int tap = 0; tap < QOA_LMS_LEN; ++tap) {
                desc.lms[channel].history[tap] = 0;
            }
        }

        const unsigned int frames = desc.samples;
        const unsigned int frame_count = (frames + QOA_FRAME_LEN - 1) / QOA_FRAME_LEN;
        const unsigned int slice_count = (frames + QOA_SLICE_LEN - 1) / QOA_SLICE_LEN;
        const std::size_t encoded_size = 8 + static_cast<std::size_t>(frame_count) * 8 +
                                         static_cast<std::size_t>(frame_count) * QOA_LMS_LEN * 4 * channels +
                                         static_cast<std::size_t>(slice_count) * 8 * channels;

        std::vector<std::byte> out(encoded_size);
        unsigned int written = qoa_encode_header(&desc, reinterpret_cast<unsigned char *>(out.data()));
        for (unsigned int frame = 0; frame < frames; frame += QOA_FRAME_LEN) {
            const unsigned int frame_len = std::min<unsigned int>(QOA_FRAME_LEN, frames - frame);
            const std::int16_t *frame_samples = interleaved.data() + static_cast<std::size_t>(frame) * channels;
            written += qoa_encode_frame(reinterpret_cast<const short *>(frame_samples), &desc, frame_len,
                                        reinterpret_cast<unsigned char *>(out.data()) + written);
        }
        out.resize(written);
        return out;
    }

    std::vector<std::byte> qoa_silence(const std::uint32_t channels, const std::uint32_t sample_rate,
                                       const std::uint32_t frames)
    {
        std::vector<std::int16_t> samples(static_cast<std::size_t>(frames) * channels, 0);
        return make_qoa_stream(channels, sample_rate, samples);
    }

    std::vector<std::byte> make_ima_mono(const std::span<const std::byte> packed)
    {
        std::vector<std::byte> out(ima_seed_bytes, std::byte{0});
        out.insert(out.end(), packed.begin(), packed.end());
        return out;
    }

    std::vector<std::byte> make_ima_stereo(const std::span<const std::byte> left_packed,
                                           const std::span<const std::byte> right_packed)
    {
        if (left_packed.size() != right_packed.size()) {
            throw std::invalid_argument("IMA fixture: stereo lanes must be the same length");
        }
        std::vector<std::byte> left(ima_seed_bytes, std::byte{0});
        left.insert(left.end(), left_packed.begin(), left_packed.end());
        std::vector<std::byte> right(ima_seed_bytes, std::byte{0});
        right.insert(right.end(), right_packed.begin(), right_packed.end());
        std::vector<std::byte> out(left.size() * 2);
        for (std::size_t index = 0; index < left.size(); ++index) {
            out[index * 2] = left[index];
            out[index * 2 + 1] = right[index];
        }
        return out;
    }
} // namespace test::support
