#include "audio_fixtures.h"

#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>

namespace test::support
{
    namespace
    {
        // 128 kbit/s, 44100 Hz, no padding: floor(144 * 128000 / 44100) bytes.
        constexpr std::uint32_t mp3_frame_bytes = 144U * 128000U / 44100U;

        [[nodiscard]] std::uint16_t read_u16le(const std::span<const std::byte> bytes, const std::size_t offset)
        {
            return static_cast<std::uint16_t>(std::to_integer<unsigned>(bytes[offset]) |
                                              (std::to_integer<unsigned>(bytes[offset + 1]) << 8));
        }

        [[nodiscard]] std::uint32_t read_u32le(const std::span<const std::byte> bytes, const std::size_t offset)
        {
            return static_cast<std::uint32_t>(std::to_integer<unsigned>(bytes[offset]) |
                                              (std::to_integer<unsigned>(bytes[offset + 1]) << 8) |
                                              (std::to_integer<unsigned>(bytes[offset + 2]) << 16) |
                                              (std::to_integer<unsigned>(bytes[offset + 3]) << 24));
        }

        [[nodiscard]] bool matches_tag(const std::span<const std::byte> bytes, const std::size_t offset,
                                       const std::string_view tag)
        {
            for (std::size_t index = 0; index < tag.size(); ++index) {
                if (bytes[offset + index] != static_cast<std::byte>(tag[index])) {
                    return false;
                }
            }
            return true;
        }

        [[noreturn]] void fail(const char *const reason)
        {
            throw std::runtime_error(std::string("invalid PCM16 WAV: ") + reason);
        }
    } // namespace

    std::vector<std::byte> make_mp3_silence(const std::uint32_t frame_count, const bool stereo)
    {
        std::vector<std::byte> out;
        out.reserve(static_cast<std::size_t>(frame_count) * mp3_frame_bytes);
        for (std::uint32_t index = 0; index < frame_count; ++index) {
            std::vector<std::byte> frame(mp3_frame_bytes, std::byte{0});
            frame[0] = std::byte{0xFF}; // sync word
            frame[1] = std::byte{0xFB}; // MPEG-1, Layer III, no CRC
            frame[2] = std::byte{0x90}; // bitrate index 9 (128 kbit/s), 44100 Hz, no padding
            frame[3] = static_cast<std::byte>(stereo ? 0x00 : 0xC0); // channel mode
            out.insert(out.end(), frame.begin(), frame.end());
        }
        return out;
    }

    wav_view parse_pcm16_wav(const std::span<const std::byte> bytes)
    {
        if (bytes.size() < 44) {
            fail("stream shorter than a header");
        }
        if (!matches_tag(bytes, 0, "RIFF") || !matches_tag(bytes, 8, "WAVE")) {
            fail("missing RIFF/WAVE signature");
        }
        if (read_u32le(bytes, 4) != bytes.size() - 8) {
            fail("RIFF chunk size does not span the stream");
        }
        if (!matches_tag(bytes, 12, "fmt ")) {
            fail("missing fmt chunk");
        }
        if (read_u32le(bytes, 16) != 16) {
            fail("unexpected fmt chunk size");
        }

        wav_view view;
        view.format_tag = read_u16le(bytes, 20);
        view.channels = read_u16le(bytes, 22);
        view.sample_rate = read_u32le(bytes, 24);
        view.byte_rate = read_u32le(bytes, 28);
        view.block_align = read_u16le(bytes, 32);
        view.bits_per_sample = read_u16le(bytes, 34);

        if (!matches_tag(bytes, 36, "data")) {
            fail("missing data chunk");
        }
        const std::uint32_t data_bytes = read_u32le(bytes, 40);
        if (data_bytes != bytes.size() - 44) {
            fail("data chunk size does not span the remainder");
        }
        view.samples.resize(data_bytes / 2);
        for (std::size_t index = 0; index < view.samples.size(); ++index) {
            view.samples[index] = static_cast<std::int16_t>(read_u16le(bytes, 44 + index * 2));
        }
        return view;
    }
} // namespace test::support
