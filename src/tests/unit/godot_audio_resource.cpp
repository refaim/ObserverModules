#include "../../core/godot/audio_resource.h"

#include "../../core/codecs/godot_ima_codec.h"
#include "../../core/codecs/qoa_codec.h"
#include "../../core/io/byte_source.h"
#include "../support/audio_fixtures.h"
#include "../support/godot_resource_fixtures.h"

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <limits>
#include <optional>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

// The QOA encoder is the pinned reference implementation; it lets the fixture produce a
// canonical valid QOA sample. Only the declarations are needed here.
#define QOA_NO_STDIO
#include <qoa.h>

#include <catch2/catch_test_macros.hpp>

namespace
{
    using unpackrat::codecs::audio_error;
    using unpackrat::codecs::audio_limits;
    using unpackrat::codecs::pcm_audio;
    using unpackrat::godot::audio_resource_extension;
    using unpackrat::godot::binary_resource;
    using unpackrat::godot::read_binary_resource;
    using unpackrat::godot::write_audio_resource;

    using test::support::build_resource;
    using test::support::godot_resource_bytes;
    using test::support::resource_document_spec;
    using test::support::resource_property_spec;
    using test::support::resource_record_spec;
    using test::support::variant_encoder;
    using test::support::wire_writer;

    std::span<const std::byte> as_bytes(const godot_resource_bytes &bytes)
    {
        return {reinterpret_cast<const std::byte *>(bytes.data()), bytes.size()};
    }

    godot_resource_bytes to_resource(const std::vector<std::byte> &bytes)
    {
        godot_resource_bytes out;
        out.reserve(bytes.size());
        for (const std::byte value : bytes) {
            out.push_back(static_cast<std::uint8_t>(value));
        }
        return out;
    }

    binary_resource read(const godot_resource_bytes &bytes)
    {
        return read_binary_resource(as_bytes(bytes));
    }

    template <typename Encode> godot_resource_bytes value_of(Encode encode)
    {
        wire_writer out(false, false);
        variant_encoder encoder(out, true, false);
        encode(encoder);
        return out.take();
    }

    resource_property_spec prop(std::string name, godot_resource_bytes value)
    {
        return resource_property_spec{std::move(name), std::move(value), std::nullopt};
    }

    godot_resource_bytes bytes_value(const std::vector<std::uint8_t> &data)
    {
        return value_of([&](variant_encoder &encoder) { encoder.bytes(data); });
    }

    godot_resource_bytes int_value(const std::int64_t value)
    {
        return value_of([&](variant_encoder &encoder) { encoder.int32(static_cast<std::int32_t>(value)); });
    }

    godot_resource_bytes bool_value(const bool value)
    {
        return value_of([&](variant_encoder &encoder) { encoder.boolean(value); });
    }

    godot_resource_bytes string_value(const std::string_view value)
    {
        return value_of([&](variant_encoder &encoder) { encoder.string(value); });
    }

    godot_resource_bytes real_value(const double value)
    {
        return value_of([&](variant_encoder &encoder) { encoder.real_value(value); });
    }

    godot_resource_bytes int64_array_value(const std::span<const std::int64_t> values)
    {
        return value_of([&](variant_encoder &encoder) { encoder.int64_array(values); });
    }

    godot_resource_bytes array_value(const std::vector<godot_resource_bytes> &elements)
    {
        return value_of([&](variant_encoder &encoder) { encoder.array(elements); });
    }

    godot_resource_bytes reference_value(const std::uint32_t index)
    {
        return value_of([&](variant_encoder &encoder) { encoder.object_internal(index); });
    }

    godot_resource_bytes null_reference_value()
    {
        return value_of([&](variant_encoder &encoder) { encoder.object_empty(); });
    }

    godot_resource_bytes external_reference_value()
    {
        return value_of(
            [&](variant_encoder &encoder) { encoder.object_external("OggPacketSequence", "res://other.res"); });
    }

    godot_resource_bytes dictionary_value(const std::vector<godot_resource_bytes> &keys_and_values)
    {
        return value_of([&](variant_encoder &encoder) { encoder.dictionary(keys_and_values); });
    }

    // A Godot 2/3/4 sample document with the four descriptor fields.
    resource_document_spec wave_document(const std::uint32_t major, const std::string &class_name,
                                         const godot_resource_bytes &data, const std::int64_t format,
                                         const std::int64_t mix_rate, const bool stereo)
    {
        resource_document_spec spec;
        spec.engine_major = major;
        spec.engine_minor = major >= 4 ? 5U : (major == 3 ? 6U : 1U);
        spec.format_version = major >= 4 ? 6U : (major == 3 ? 3U : 1U);
        spec.main_type = class_name;
        resource_record_spec record;
        record.path = "";
        record.type = class_name;
        record.properties = {
            prop("data", bytes_value(data)),
            prop("format", int_value(format)),
            prop("mix_rate", int_value(mix_rate)),
            prop("stereo", bool_value(stereo)),
        };
        spec.records = {record};
        return spec;
    }

    // A Godot 2 ``Sample`` document; the record owns ``mix_rate`` and a ``data`` dictionary.
    resource_document_spec sample_document(const godot_resource_bytes &data, const std::string_view packing,
                                           const std::string_view format, const bool stereo, const std::int64_t length,
                                           const std::int64_t mix_rate)
    {
        resource_document_spec spec;
        spec.engine_major = 2;
        spec.engine_minor = 1;
        spec.format_version = 1;
        spec.main_type = "Sample";
        spec.string_table = {"mix_rate", "data"};
        resource_record_spec record;
        record.path = "";
        record.type = "Sample";
        record.properties = {
            prop("mix_rate", int_value(mix_rate)),
            prop("data", dictionary_value({string_value("packing"), string_value(packing), string_value("format"),
                                           string_value(format), string_value("stereo"), bool_value(stereo),
                                           string_value("length"), int_value(length), string_value("data"),
                                           bytes_value(data)})),
        };
        spec.records = {record};
        return spec;
    }

    resource_document_spec text_document(const std::string &class_name, const godot_resource_bytes &data)
    {
        resource_document_spec spec;
        spec.engine_major = 4;
        spec.engine_minor = 5;
        spec.format_version = 6;
        spec.main_type = class_name;
        resource_record_spec record;
        record.path = "";
        record.type = class_name;
        record.properties = {prop("data", bytes_value(data))};
        spec.records = {record};
        return spec;
    }

    // A Godot 4 ``AudioStreamOggVorbis`` whose ``packet_sequence`` references an internal record.
    resource_document_spec ogg_document(const std::vector<std::vector<std::vector<std::uint8_t>>> &pages,
                                        const std::vector<std::int64_t> &granules, const double sampling_rate,
                                        const godot_resource_bytes &reference = reference_value(0))
    {
        std::vector<godot_resource_bytes> page_values;
        for (const auto &page : pages) {
            std::vector<godot_resource_bytes> packets;
            for (const auto &packet : page) {
                packets.push_back(bytes_value(packet));
            }
            page_values.push_back(array_value(packets));
        }
        resource_document_spec spec;
        spec.engine_major = 4;
        spec.engine_minor = 5;
        spec.format_version = 6;
        spec.named_scene_ids = true;
        spec.main_type = "AudioStreamOggVorbis";
        resource_record_spec sequence;
        sequence.path = "local://1";
        sequence.type = "OggPacketSequence";
        sequence.properties = {
            prop("packet_data", array_value(page_values)),
            prop("granule_positions", int64_array_value(granules)),
            prop("sampling_rate", real_value(sampling_rate)),
        };
        resource_record_spec root;
        root.path = "";
        root.type = "AudioStreamOggVorbis";
        root.properties = {prop("packet_sequence", reference)};
        spec.records = {sequence, root};
        return spec;
    }

    std::optional<std::string_view> extension_of(const binary_resource &resource, const audio_limits &limits = {})
    {
        return audio_resource_extension(resource, limits);
    }

    std::vector<std::byte> collect(const binary_resource &resource, const audio_limits &limits = {})
    {
        std::vector<std::byte> output;
        write_audio_resource(
            resource,
            [&output](const std::span<const std::byte> chunk) {
                output.insert(output.end(), chunk.begin(), chunk.end());
            },
            limits);
        if (const auto info = unpackrat::godot::inspect_audio_resource(resource, limits); info.has_value()) {
            if (info.value().output_size.has_value()) {
                CHECK(info.value().output_size == output.size());
            } else {
                CHECK(info.value().extension == "ogg"); // a packet sequence needs a bounded mux pass
            }
        } else {
            FAIL("a successful audio write must have an inspectable descriptor");
        }
        return output;
    }

    struct parsed_wave
    {
        std::uint16_t format = 0;
        std::uint16_t channels = 0;
        std::uint32_t sample_rate = 0;
        std::uint16_t bits = 0;
        std::vector<std::int16_t> samples;
    };

    parsed_wave parse_wave(const std::vector<std::byte> &bytes)
    {
        REQUIRE(bytes.size() >= 44);
        const auto byte_at = [&](const std::size_t index) { return std::to_integer<unsigned>(bytes[index]); };
        const auto u16 = [&](const std::size_t index) {
            return static_cast<std::uint16_t>(byte_at(index) | (byte_at(index + 1) << 8U));
        };
        const auto u32 = [&](const std::size_t index) {
            return static_cast<std::uint32_t>(byte_at(index)) | (static_cast<std::uint32_t>(byte_at(index + 1)) << 8U) |
                   (static_cast<std::uint32_t>(byte_at(index + 2)) << 16U) |
                   (static_cast<std::uint32_t>(byte_at(index + 3)) << 24U);
        };
        REQUIRE(std::memcmp(bytes.data(), "RIFF", 4) == 0);
        REQUIRE(std::memcmp(bytes.data() + 8, "WAVE", 4) == 0);

        parsed_wave wave;
        std::size_t position = 12;
        while (position + 8 <= bytes.size()) {
            const std::string id(reinterpret_cast<const char *>(bytes.data()) + position, 4);
            const std::uint32_t size = u32(position + 4);
            REQUIRE(position + 8 + size <= bytes.size());
            if (id == "fmt ") {
                wave.format = u16(position + 8);
                wave.channels = u16(position + 10);
                wave.sample_rate = u32(position + 12);
                wave.bits = u16(position + 22);
            } else if (id == "data") {
                for (std::size_t index = 0; index < size / 2; ++index) {
                    wave.samples.push_back(static_cast<std::int16_t>(u16(position + 8 + index * 2)));
                }
            }
            position += 8 + size + (size & 1U);
        }
        return wave;
    }

    // Parses an Ogg stream into its page granules and the BOS flag of the first page.
    struct parsed_ogg
    {
        bool first_page_bos = false;
        std::vector<std::int64_t> granules;
    };

    parsed_ogg parse_ogg(const std::vector<std::byte> &bytes)
    {
        REQUIRE(bytes.size() >= 27);
        REQUIRE(std::memcmp(bytes.data(), "OggS", 4) == 0);
        REQUIRE(std::to_integer<unsigned>(bytes[4]) == 0);

        parsed_ogg ogg;
        std::size_t position = 0;
        while (position + 27 <= bytes.size()) {
            REQUIRE(std::memcmp(bytes.data() + position, "OggS", 4) == 0);
            const std::size_t segments = std::to_integer<unsigned>(bytes[position + 26]);
            std::size_t body = 0;
            for (std::size_t index = 0; index < segments; ++index) {
                body += std::to_integer<unsigned>(bytes[position + 27 + index]);
            }
            std::uint64_t granule = 0;
            for (std::size_t index = 0; index < 8; ++index) {
                granule |= static_cast<std::uint64_t>(std::to_integer<unsigned>(bytes[position + 6 + index]))
                           << (8 * index);
            }
            ogg.granules.push_back(static_cast<std::int64_t>(granule));
            if (position == 0) {
                ogg.first_page_bos = (std::to_integer<unsigned>(bytes[position + 5]) & 0x02U) != 0;
            }
            position += 27 + segments + body;
        }
        REQUIRE(position == bytes.size());
        return ogg;
    }

    bool contains(const std::vector<std::byte> &haystack, const std::vector<std::uint8_t> &needle)
    {
        const auto found =
            std::search(haystack.begin(), haystack.end(), reinterpret_cast<const std::byte *>(needle.data()),
                        reinterpret_cast<const std::byte *>(needle.data()) + needle.size());
        return found != haystack.end();
    }

    // A canonical QOA stream, encoded by the pinned reference encoder.
    std::vector<std::byte> make_qoa(const std::uint32_t channels, const std::uint32_t sample_rate,
                                    const std::span<const std::int16_t> interleaved)
    {
        qoa_desc desc{};
        desc.channels = channels;
        desc.samplerate = sample_rate;
        desc.samples = static_cast<unsigned>(interleaved.size() / channels);
        for (std::uint32_t channel = 0; channel < channels; ++channel) {
            desc.lms[channel].weights[0] = 0;
            desc.lms[channel].weights[1] = 0;
            desc.lms[channel].weights[2] = -static_cast<int>(1U << 13U);
            desc.lms[channel].weights[3] = static_cast<int>(1U << 14U);
            for (int tap = 0; tap < QOA_LMS_LEN; ++tap) {
                desc.lms[channel].history[tap] = 0;
            }
        }
        const unsigned frames = desc.samples;
        const unsigned frame_count = (frames + QOA_FRAME_LEN - 1) / QOA_FRAME_LEN;
        const unsigned slice_count = (frames + QOA_SLICE_LEN - 1) / QOA_SLICE_LEN;
        const std::size_t size = 8 + static_cast<std::size_t>(frame_count) * 8 +
                                 static_cast<std::size_t>(frame_count) * QOA_LMS_LEN * 4 * channels +
                                 static_cast<std::size_t>(slice_count) * 8 * channels;
        std::vector<std::byte> out(size);
        unsigned written = qoa_encode_header(&desc, reinterpret_cast<unsigned char *>(out.data()));
        for (unsigned frame = 0; frame < frames; frame += QOA_FRAME_LEN) {
            const unsigned frame_len = (frames - frame) < QOA_FRAME_LEN ? (frames - frame) : QOA_FRAME_LEN;
            written +=
                qoa_encode_frame(reinterpret_cast<const short *>(interleaved.data() + std::size_t{frame} * channels),
                                 &desc, frame_len, reinterpret_cast<unsigned char *>(out.data()) + written);
        }
        out.resize(written);
        return out;
    }

    // The Godot IMA ADPCM resource payload is headerless: every stored byte is packed nibble data,
    // so the fixture must not fabricate a leading seed. Godot's writer happens to prefix each lane
    // with four zero bytes when producing a stream, but those bytes are ordinary data and are not
    // part of the stored ``data`` field this resource carries.
    std::vector<std::byte> make_ima_mono(const std::span<const std::uint8_t> packed)
    {
        std::vector<std::byte> out;
        out.reserve(packed.size());
        for (const std::uint8_t value : packed) {
            out.push_back(static_cast<std::byte>(value));
        }
        return out;
    }

    // An independent Godot IMA ADPCM reference decoder, written directly from the documented
    // nibble order and step/index tables (Godot 4.5 scene/resources/audio_stream_wav.cpp and the
    // independently developed gdre sample exporter). It deliberately does not call the production
    // codec, so a fixture whose expected PCM is derived here cannot be masked by a codec bug.
    std::vector<std::int16_t> ima_reference_pcm(const std::span<const std::uint8_t> packed, const unsigned channels)
    {
        static constexpr std::int32_t step_table[89] = {
            7,    8,     9,     10,    11,    12,    13,    14,    16,    17,    19,    21,    23,    25,    28,
            31,   34,    37,    41,    45,    50,    55,    60,    66,    73,    80,    88,    97,    107,   118,
            130,  143,   157,   173,   190,   209,   230,   253,   279,   307,   337,   371,   408,   449,   494,
            544,  598,   658,   724,   796,   876,   963,   1060,  1166,  1282,  1411,  1552,  1707,  1878,  2066,
            2272, 2499,  2749,  3024,  3327,  3660,  4026,  4428,  4871,  5358,  5894,  6484,  7132,  7845,  8630,
            9493, 10442, 11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
        };
        static constexpr std::int32_t index_table[16] = {
            -1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8,
        };
        REQUIRE((channels == 1 || channels == 2));
        const std::size_t frames = packed.size() * 2 / channels;
        std::vector<std::int16_t> out(frames * channels, 0);
        std::int32_t predictor[2] = {0, 0};
        std::int32_t step_index[2] = {0, 0};
        for (std::size_t frame = 0; frame < frames; ++frame) {
            for (unsigned channel = 0; channel < channels; ++channel) {
                const std::size_t byte_index = (frame >> 1U) * channels + channel;
                const unsigned packed_byte = packed[byte_index];
                const std::int32_t nibble = (frame & 1U) != 0 ? static_cast<std::int32_t>(packed_byte >> 4U)
                                                              : static_cast<std::int32_t>(packed_byte & 0x0FU);
                const std::int32_t step = step_table[static_cast<std::size_t>(step_index[channel])];
                std::int32_t index = step_index[channel] + index_table[static_cast<std::size_t>(nibble)];
                index = index < 0 ? 0 : (index > 88 ? 88 : index);
                step_index[channel] = index;
                std::int32_t difference = static_cast<std::int32_t>(static_cast<std::uint32_t>(step) >> 3U);
                if ((static_cast<std::uint32_t>(nibble) & 1U) != 0) {
                    difference += static_cast<std::int32_t>(static_cast<std::uint32_t>(step) >> 2U);
                }
                if ((static_cast<std::uint32_t>(nibble) & 2U) != 0) {
                    difference += static_cast<std::int32_t>(static_cast<std::uint32_t>(step) >> 1U);
                }
                if ((static_cast<std::uint32_t>(nibble) & 4U) != 0) {
                    difference += step;
                }
                if ((static_cast<std::uint32_t>(nibble) & 8U) != 0) {
                    difference = -difference;
                }
                predictor[channel] += difference;
                if (predictor[channel] < -0x8000) {
                    predictor[channel] = -0x8000;
                } else if (predictor[channel] > 0x7FFF) {
                    predictor[channel] = 0x7FFF;
                }
                out[frame * channels + channel] = static_cast<std::int16_t>(predictor[channel]);
            }
        }
        return out;
    }

    std::vector<std::uint8_t> vorbis_identification(const std::uint8_t channels, const std::uint32_t rate,
                                                    const std::uint32_t version = 0, const std::uint8_t framing = 1)
    {
        std::vector<std::uint8_t> bytes(30, 0);
        bytes[0] = 1;
        std::memcpy(bytes.data() + 1, "vorbis", 6);
        bytes[7] = static_cast<std::uint8_t>(version);
        bytes[11] = channels;
        bytes[12] = static_cast<std::uint8_t>(rate);
        bytes[13] = static_cast<std::uint8_t>(rate >> 8U);
        bytes[14] = static_cast<std::uint8_t>(rate >> 16U);
        bytes[15] = static_cast<std::uint8_t>(rate >> 24U);
        bytes[28] = 0xB8;
        bytes[29] = framing;
        return bytes;
    }

    std::vector<std::uint8_t> vorbis_comment()
    {
        std::vector<std::uint8_t> bytes(16, 0);
        bytes[0] = 3;
        std::memcpy(bytes.data() + 1, "vorbis", 6);
        bytes[15] = 1;
        return bytes;
    }

    std::vector<std::uint8_t> vorbis_setup()
    {
        return {5, 'v', 'o', 'r', 'b', 'i', 's', 0, 0};
    }

    resource_document_spec ogg_from_values(const godot_resource_bytes &packet_data,
                                           const godot_resource_bytes &granules,
                                           const godot_resource_bytes &sampling_rate,
                                           const godot_resource_bytes &reference = reference_value(0),
                                           const std::string &sequence_class = "OggPacketSequence")
    {
        resource_document_spec spec;
        spec.engine_major = 4;
        spec.engine_minor = 5;
        spec.format_version = 6;
        spec.named_scene_ids = true;
        spec.main_type = "AudioStreamOggVorbis";
        resource_record_spec sequence;
        sequence.path = "local://1";
        sequence.type = sequence_class;
        sequence.properties = {
            prop("packet_data", packet_data),
            prop("granule_positions", granules),
            prop("sampling_rate", sampling_rate),
        };
        resource_record_spec root;
        root.path = "";
        root.type = "AudioStreamOggVorbis";
        root.properties = {prop("packet_sequence", reference)};
        spec.records = {sequence, root};
        return spec;
    }

    godot_resource_bytes single_page(const std::vector<std::vector<std::uint8_t>> &packets)
    {
        std::vector<godot_resource_bytes> values;
        values.reserve(packets.size());
        for (const auto &packet : packets) {
            values.push_back(bytes_value(packet));
        }
        return array_value({array_value(values)});
    }

    std::vector<std::uint8_t> to_unsigned(const std::vector<std::byte> &bytes)
    {
        std::vector<std::uint8_t> out(bytes.size());
        for (std::size_t index = 0; index < bytes.size(); ++index) {
            out[index] = std::to_integer<std::uint8_t>(bytes[index]);
        }
        return out;
    }

    // A complete, individually valid MPEG-1 Layer III frame (128 kbit/s, 44100 Hz, no padding,
    // 417 bytes) built by the repository-owned MP3 fixture. The previous 16-byte placeholder only
    // carried a plausible four-byte frame header and was therefore not a decodable frame at all.
    std::vector<std::uint8_t> complete_mp3_frame()
    {
        return to_unsigned(test::support::make_mp3_silence(1, false));
    }

    // A frame of ``size`` bytes whose first four bytes are a chosen MPEG-1 frame header
    // (``byte1`` selects the version/layer bits) followed by zeros. Used to place a complete-sized
    // but non-Layer-III frame in front of the recognizer.
    std::vector<std::uint8_t> mp3_sized_frame(const std::uint8_t byte1, const std::size_t size)
    {
        std::vector<std::uint8_t> frame(size, 0);
        frame[0] = 0xFF;
        frame[1] = byte1;
        frame[2] = 0x90; // bitrate index 9, 44100 Hz, no padding
        frame[3] = 0xC0; // single channel
        return frame;
    }

    // A free-format MPEG-1 Layer III stream: the repository-owned fixture's 417-byte silence frames
    // with the bitrate index cleared to 0. A free-format frame carries no bitrate, so the decoder
    // learns the frame size from the sync-word spacing and confirms it across the matching frames.
    std::vector<std::uint8_t> make_free_format_mp3(const std::uint32_t frame_count, const bool stereo)
    {
        std::vector<std::uint8_t> stream = to_unsigned(test::support::make_mp3_silence(frame_count, stereo));
        const std::size_t frame_bytes = stream.size() / frame_count;
        for (std::size_t offset = 0; offset + 4 <= stream.size(); offset += frame_bytes) {
            stream[offset + 2] = 0x00; // bitrate index 0 (free format), 44100 Hz, no padding
        }
        return stream;
    }
} // namespace

TEST_CASE("godot audio: godot 4 PCM8 sample expands to a PCM16 WAVE", "[godot][audio]")
{
    const godot_resource_bytes raw = {0x00, 0x7F, 0x80, 0xFF};
    const auto document = wave_document(4, "AudioStreamWAV", raw, 0, 8000, false);
    const binary_resource resource = read(build_resource(document));
    REQUIRE(extension_of(resource) == std::optional<std::string_view>("wav"));

    const parsed_wave wave = parse_wave(collect(resource));
    REQUIRE(wave.format == 1);
    REQUIRE(wave.bits == 16);
    REQUIRE(wave.channels == 1);
    REQUIRE(wave.sample_rate == 8000);
    REQUIRE(wave.samples == std::vector<std::int16_t>({0, 32512, -32768, -256}));
}

TEST_CASE("godot audio: omitted Godot 3/4 wave properties use the engine defaults", "[godot][audio][audio-defaults]")
{
    const godot_resource_bytes raw = {0x00, 0x7F, 0x80, 0xFF};
    for (const std::uint32_t major : {3U, 4U}) {
        for (unsigned omitted = 0; omitted < 8; ++omitted) {
            CAPTURE(major, omitted);
            auto document =
                wave_document(major, major == 4 ? "AudioStreamWAV" : "AudioStreamSample", raw, 0, 44100, false);
            std::erase_if(document.records[0].properties, [omitted](const resource_property_spec &property) {
                return ((omitted & 1U) != 0 && property.name == "format") ||
                       ((omitted & 2U) != 0 && property.name == "mix_rate") ||
                       ((omitted & 4U) != 0 && property.name == "stereo");
            });
            const binary_resource resource = read(build_resource(document));
            REQUIRE(extension_of(resource) == std::optional<std::string_view>("wav"));
            const parsed_wave wave = parse_wave(collect(resource));
            REQUIRE(wave.bits == 16);
            REQUIRE(wave.channels == 1);
            REQUIRE(wave.sample_rate == 44100);
            REQUIRE(wave.samples == std::vector<std::int16_t>({0, 32512, -32768, -256}));
        }
    }
}

TEST_CASE("godot audio: QOA descriptors may omit only default channel and rate properties",
          "[godot][audio][audio-defaults]")
{
    struct descriptor_case final
    {
        std::uint32_t channels;
        std::uint32_t sample_rate;
        bool omit_rate;
        bool omit_stereo;
        bool valid;
    };
    const descriptor_case cases[] = {
        {1, 44100, true, true, true},  {1, 22050, false, true, true}, {2, 44100, true, false, true},
        {2, 44100, true, true, false}, {1, 48000, true, true, false},
    };
    const std::vector<std::int16_t> pcm = {10, -10, 20, -20, 30, -30, 40, -40};
    for (const descriptor_case &item : cases) {
        CAPTURE(item.channels, item.sample_rate, item.omit_rate, item.omit_stereo, item.valid);
        const auto qoa = make_qoa(item.channels, item.sample_rate, pcm);
        const auto raw = to_resource(qoa);
        auto document = wave_document(4, "AudioStreamWAV", raw, 3, item.sample_rate, item.channels == 2);
        std::erase_if(document.records[0].properties, [&item](const resource_property_spec &property) {
            return (item.omit_rate && property.name == "mix_rate") || (item.omit_stereo && property.name == "stereo");
        });
        const binary_resource resource = read(build_resource(document));
        if (!item.valid) {
            REQUIRE_THROWS_AS(extension_of(resource), audio_error);
            REQUIRE_THROWS_AS(collect(resource), audio_error);
            continue;
        }
        REQUIRE(extension_of(resource) == std::optional<std::string_view>("wav"));
        const parsed_wave wave = parse_wave(collect(resource));
        REQUIRE(wave.channels == item.channels);
        REQUIRE(wave.sample_rate == item.sample_rate);
        REQUIRE(wave.samples == unpackrat::codecs::decode_qoa(as_bytes(raw)).samples);
    }
}

TEST_CASE("godot audio: explicit wave properties retain type and uniqueness validation",
          "[godot][audio][audio-defaults]")
{
    for (const std::uint32_t major : {3U, 4U}) {
        for (const std::string_view name : {"format", "mix_rate", "stereo"}) {
            for (const bool duplicate : {false, true}) {
                CAPTURE(major, name, duplicate);
                auto document = wave_document(major, major == 4 ? "AudioStreamWAV" : "AudioStreamSample", {0x00, 0x01},
                                              0, 44100, false);
                if (duplicate) {
                    const auto found =
                        std::find_if(document.records[0].properties.begin(), document.records[0].properties.end(),
                                     [name](const auto &property) { return property.name == name; });
                    REQUIRE(found != document.records[0].properties.end());
                    document.records[0].properties.push_back(*found);
                } else {
                    for (auto &property : document.records[0].properties) {
                        if (property.name == name)
                            property.value = string_value("invalid");
                    }
                }
                const binary_resource resource = read(build_resource(document));
                REQUIRE_THROWS_AS(extension_of(resource), audio_error);
                REQUIRE_THROWS_AS(collect(resource), audio_error);
            }
        }
    }
}

TEST_CASE("godot audio: wave defaults do not relax the Godot 2 Sample rate type", "[godot][audio][audio-defaults]")
{
    // Sample uses its own required descriptor; defaults for the Godot 3/4 wave classes must
    // neither coerce a different variant type nor substitute for an explicitly invalid rate.
    for (const godot_resource_bytes &invalid : {bool_value(false), real_value(44100.0), string_value("44100")}) {
        auto document = sample_document({0x00}, "raw", "pcm8", false, 1, 8000);
        document.records[0].properties[0].value = invalid;
        const binary_resource resource = read(build_resource(document));
        REQUIRE_THROWS_AS(extension_of(resource), audio_error);
        REQUIRE_THROWS_AS(collect(resource), audio_error);
    }
}

TEST_CASE("godot audio: godot 4 stereo PCM8 sample keeps both lanes", "[godot][audio]")
{
    const godot_resource_bytes raw = {0x01, 0x02, 0x03, 0x04};
    const auto document = wave_document(4, "AudioStreamWAV", raw, 0, 44100, true);
    const binary_resource resource = read(build_resource(document));
    const parsed_wave wave = parse_wave(collect(resource));
    REQUIRE(wave.channels == 2);
    REQUIRE(wave.samples == std::vector<std::int16_t>({256, 512, 768, 1024}));
}

TEST_CASE("godot audio: godot 4 PCM16 sample is copied verbatim", "[godot][audio]")
{
    const godot_resource_bytes raw = {0x34, 0x12, 0xFE, 0xFF, 0x00, 0x80, 0xFF, 0x7F};
    const auto document = wave_document(4, "AudioStreamWAV", raw, 1, 22050, true);
    const binary_resource resource = read(build_resource(document));
    REQUIRE(extension_of(resource) == std::optional<std::string_view>("wav"));
    const parsed_wave wave = parse_wave(collect(resource));
    REQUIRE(wave.channels == 2);
    REQUIRE(wave.sample_rate == 22050);
    REQUIRE(wave.samples == std::vector<std::int16_t>({0x1234, -2, -32768, 32767}));
}

TEST_CASE("godot audio: godot 3 AudioStreamSample PCM16 decodes", "[godot][audio]")
{
    const godot_resource_bytes raw = {0x01, 0x00, 0x02, 0x00};
    const auto document = wave_document(3, "AudioStreamSample", raw, 1, 16000, false);
    const binary_resource resource = read(build_resource(document));
    REQUIRE(extension_of(resource) == std::optional<std::string_view>("wav"));
    const parsed_wave wave = parse_wave(collect(resource));
    REQUIRE(wave.samples == std::vector<std::int16_t>({1, 2}));
}

TEST_CASE("godot audio: godot 4 IMA ADPCM sample decodes to the independent reference PCM", "[godot][audio]")
{
    const std::vector<std::uint8_t> packed = {0x11, 0x22, 0x33, 0x44, 0x55};
    const std::vector<std::byte> payload = make_ima_mono(packed);
    const godot_resource_bytes raw = to_resource(payload);
    const auto document = wave_document(4, "AudioStreamWAV", raw, 2, 32000, false);
    const binary_resource resource = read(build_resource(document));
    REQUIRE(extension_of(resource) == std::optional<std::string_view>("wav"));

    const parsed_wave wave = parse_wave(collect(resource));
    REQUIRE(wave.samples == ima_reference_pcm(packed, 1));
}

TEST_CASE("godot audio: godot 4 QOA sample decodes to the same PCM as the codec", "[godot][audio]")
{
    const std::vector<std::int16_t> pcm = {0, 1000, -1000, 32000, -32000, 5, -5, 7};
    const std::vector<std::byte> qoa = make_qoa(1, 48000, pcm);
    const godot_resource_bytes raw = to_resource(qoa);
    const auto document = wave_document(4, "AudioStreamWAV", raw, 3, 48000, false);
    const binary_resource resource = read(build_resource(document));
    REQUIRE(extension_of(resource) == std::optional<std::string_view>("wav"));

    const std::vector<std::int16_t> expected = unpackrat::codecs::decode_qoa(as_bytes(raw)).samples;
    const parsed_wave wave = parse_wave(collect(resource));
    REQUIRE(wave.sample_rate == 48000);
    REQUIRE(wave.samples == expected);
}

TEST_CASE("godot audio: stereo QOA sample validates its header against the descriptor", "[godot][audio]")
{
    const std::vector<std::int16_t> pcm = {10, -10, 20, -20, 30, -30, 40, -40};
    const std::vector<std::byte> qoa = make_qoa(2, 44100, pcm);
    const godot_resource_bytes raw = to_resource(qoa);
    const auto document = wave_document(4, "AudioStreamWAV", raw, 3, 44100, true);
    const binary_resource resource = read(build_resource(document));
    REQUIRE(extension_of(resource) == std::optional<std::string_view>("wav"));
    const parsed_wave wave = parse_wave(collect(resource));
    REQUIRE(wave.channels == 2);
    REQUIRE(wave.samples == unpackrat::codecs::decode_qoa(as_bytes(raw)).samples);
}

TEST_CASE("godot audio: an unknown sample format resolves to no extension", "[godot][audio]")
{
    const godot_resource_bytes raw = {0x00, 0x11};
    const auto document = wave_document(4, "AudioStreamWAV", raw, 9, 8000, false);
    const binary_resource resource = read(build_resource(document));
    REQUIRE(!(extension_of(resource).has_value()));
    REQUIRE_THROWS_AS(collect(resource), audio_error);
}

TEST_CASE("godot audio: a misaligned sample payload is malformed", "[godot][audio]")
{
    SECTION("8-bit stereo with an odd byte count")
    {
        const godot_resource_bytes raw = {0x00, 0x11, 0x22};
        const binary_resource resource = read(build_resource(wave_document(4, "AudioStreamWAV", raw, 0, 8000, true)));
        REQUIRE_THROWS_AS(extension_of(resource), audio_error);
    }
    SECTION("16-bit with an odd byte count")
    {
        const godot_resource_bytes raw = {0x00, 0x11, 0x22};
        const binary_resource resource = read(build_resource(wave_document(4, "AudioStreamWAV", raw, 1, 8000, false)));
        REQUIRE_THROWS_AS(collect(resource), audio_error);
    }
}

namespace
{
    godot_resource_bytes as_resource(const std::vector<std::uint8_t> &bytes)
    {
        return godot_resource_bytes(bytes.begin(), bytes.end());
    }

    // A Godot 2 ``Sample`` with an arbitrary ``data`` value so malformed dictionaries can be built.
    resource_document_spec sample_from(const godot_resource_bytes &data_value, const std::int64_t mix_rate)
    {
        resource_document_spec spec;
        spec.engine_major = 2;
        spec.engine_minor = 1;
        spec.format_version = 1;
        spec.main_type = "Sample";
        spec.string_table = {"mix_rate", "data"};
        resource_record_spec record;
        record.path = "";
        record.type = "Sample";
        record.properties = {prop("mix_rate", int_value(mix_rate)), prop("data", data_value)};
        spec.records = {record};
        return spec;
    }

    godot_resource_bytes sample_dictionary(const std::vector<godot_resource_bytes> &keys_and_values)
    {
        return dictionary_value(keys_and_values);
    }
} // namespace

TEST_CASE("godot audio: godot 2 Sample PCM16 decodes to WAVE", "[godot][audio]")
{
    const godot_resource_bytes raw = {0x10, 0x00, 0xF0, 0xFF};
    const auto document = sample_document(raw, "raw", "pcm16", false, 2, 11025);
    const binary_resource resource = read(build_resource(document));
    REQUIRE(extension_of(resource) == std::optional<std::string_view>("wav"));
    const parsed_wave wave = parse_wave(collect(resource));
    REQUIRE(wave.channels == 1);
    REQUIRE(wave.sample_rate == 11025);
    REQUIRE(wave.samples == std::vector<std::int16_t>({16, -16}));
}

TEST_CASE("godot audio: godot 2 Sample PCM8 stereo decodes to WAVE", "[godot][audio]")
{
    const godot_resource_bytes raw = {0x7F, 0x80, 0x01, 0xFF};
    const auto document = sample_document(raw, "raw", "pcm8", true, 2, 8000);
    const binary_resource resource = read(build_resource(document));
    const parsed_wave wave = parse_wave(collect(resource));
    REQUIRE(wave.channels == 2);
    REQUIRE(wave.samples == std::vector<std::int16_t>({32512, -32768, 256, -256}));
}

TEST_CASE("godot audio: godot 2 Sample IMA ADPCM decodes to the independent reference PCM", "[godot][audio]")
{
    const std::vector<std::uint8_t> packed = {0x01, 0x23, 0x45};
    const std::vector<std::byte> payload = make_ima_mono(packed);
    const godot_resource_bytes raw = to_resource(payload);
    // Three headerless bytes carry six mono nibbles, so the declared frame count is six.
    const auto document = sample_document(raw, "raw", "ima_adpcm", false, 6, 44100);
    const binary_resource resource = read(build_resource(document));
    REQUIRE(extension_of(resource) == std::optional<std::string_view>("wav"));
    REQUIRE(parse_wave(collect(resource)).samples == ima_reference_pcm(packed, 1));
}

TEST_CASE("godot audio: a Godot 2 Sample with an unsupported variant resolves to no extension", "[godot][audio]")
{
    SECTION("packing other than raw")
    {
        const auto document = sample_document({0x00}, "compressed", "pcm8", false, 1, 8000);
        const binary_resource resource = read(build_resource(document));
        REQUIRE(!(extension_of(resource).has_value()));
        REQUIRE_THROWS_AS(collect(resource), audio_error);
    }
    SECTION("format other than the known names")
    {
        const auto document = sample_document({0x00}, "raw", "adpcm_x", false, 1, 8000);
        REQUIRE(!(extension_of(read(build_resource(document))).has_value()));
    }
}

TEST_CASE("godot audio: malformed Godot 2 Sample descriptors are rejected", "[godot][audio]")
{
    SECTION("negative length")
    {
        const auto document = sample_document({0x00}, "raw", "pcm8", false, -1, 8000);
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document))), audio_error);
    }
    SECTION("8-bit length mismatch")
    {
        const auto document = sample_document({0x00, 0x11}, "raw", "pcm8", false, 3, 8000);
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document))), audio_error);
    }
    SECTION("16-bit length mismatch")
    {
        const auto document = sample_document({0x00, 0x11}, "raw", "pcm16", false, 3, 8000);
        REQUIRE_THROWS_AS(collect(read(build_resource(document))), audio_error);
    }
    SECTION("missing dictionary key")
    {
        const godot_resource_bytes dictionary = sample_dictionary(
            {string_value("packing"), string_value("raw"), string_value("format"), string_value("pcm8"),
             string_value("stereo"), bool_value(false), string_value("data"), bytes_value({0x00})});
        REQUIRE_THROWS_AS(extension_of(read(build_resource(sample_from(dictionary, 8000)))), audio_error);
    }
    SECTION("string key with the wrong type")
    {
        const godot_resource_bytes dictionary =
            sample_dictionary({string_value("packing"), int_value(1), string_value("format"), string_value("pcm8"),
                               string_value("stereo"), bool_value(false), string_value("length"), int_value(1),
                               string_value("data"), bytes_value({0x00})});
        REQUIRE_THROWS_AS(extension_of(read(build_resource(sample_from(dictionary, 8000)))), audio_error);
    }
    SECTION("integer key with the wrong type")
    {
        const godot_resource_bytes dictionary =
            sample_dictionary({string_value("packing"), string_value("raw"), string_value("format"),
                               string_value("pcm8"), string_value("stereo"), bool_value(false), string_value("length"),
                               string_value("one"), string_value("data"), bytes_value({0x00})});
        REQUIRE_THROWS_AS(extension_of(read(build_resource(sample_from(dictionary, 8000)))), audio_error);
    }
    SECTION("boolean key with the wrong type")
    {
        const godot_resource_bytes dictionary =
            sample_dictionary({string_value("packing"), string_value("raw"), string_value("format"),
                               string_value("pcm8"), string_value("stereo"), int_value(0), string_value("length"),
                               int_value(1), string_value("data"), bytes_value({0x00})});
        REQUIRE_THROWS_AS(extension_of(read(build_resource(sample_from(dictionary, 8000)))), audio_error);
    }
    SECTION("byte key with the wrong type")
    {
        const godot_resource_bytes dictionary =
            sample_dictionary({string_value("packing"), string_value("raw"), string_value("format"),
                               string_value("pcm8"), string_value("stereo"), bool_value(false), string_value("length"),
                               int_value(1), string_value("data"), int_value(0)});
        REQUIRE_THROWS_AS(extension_of(read(build_resource(sample_from(dictionary, 8000)))), audio_error);
    }
    SECTION("data is not a dictionary")
    {
        REQUIRE_THROWS_AS(extension_of(read(build_resource(sample_from(int_value(3), 8000)))), audio_error);
    }
    SECTION("missing mix_rate")
    {
        const auto document = sample_document({0x00}, "raw", "pcm8", false, 1, 8000);
        auto broken = document;
        broken.records[0].properties.erase(broken.records[0].properties.begin());
        REQUIRE_THROWS_AS(extension_of(read(build_resource(broken))), audio_error);
    }
}

TEST_CASE("godot audio: malformed Godot 3/4 sample descriptors are rejected", "[godot][audio]")
{
    const godot_resource_bytes raw = {0x00, 0x11, 0x22, 0x33};
    SECTION("missing data field")
    {
        auto document = wave_document(4, "AudioStreamWAV", raw, 0, 8000, false);
        document.records[0].properties.erase(document.records[0].properties.begin());
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document))), audio_error);
    }
    SECTION("format field with the wrong type")
    {
        auto document = wave_document(4, "AudioStreamWAV", raw, 0, 8000, false);
        document.records[0].properties[1].value = string_value("pcm");
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document))), audio_error);
    }
    SECTION("data field with the wrong type")
    {
        auto document = wave_document(4, "AudioStreamWAV", raw, 0, 8000, false);
        document.records[0].properties[0].value = int_value(7);
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document))), audio_error);
    }
    SECTION("stereo field with the wrong type")
    {
        auto document = wave_document(4, "AudioStreamWAV", raw, 0, 8000, false);
        document.records[0].properties[3].value = int_value(0);
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document))), audio_error);
    }
    SECTION("duplicate data field")
    {
        auto document = wave_document(4, "AudioStreamWAV", raw, 0, 8000, false);
        document.records[0].properties.push_back(prop("data", bytes_value(raw)));
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document))), audio_error);
    }
    SECTION("zero sample rate")
    {
        REQUIRE_THROWS_AS(extension_of(read(build_resource(wave_document(4, "AudioStreamWAV", raw, 1, 0, false)))),
                          audio_error);
    }
    SECTION("negative sample rate")
    {
        REQUIRE_THROWS_AS(extension_of(read(build_resource(wave_document(4, "AudioStreamWAV", raw, 1, -1, false)))),
                          audio_error);
    }
    SECTION("out-of-range sample rate")
    {
        REQUIRE_THROWS_AS(
            extension_of(read(build_resource(wave_document(4, "AudioStreamWAV", raw, 1, 0x1000000, false)))),
            audio_error);
    }
    SECTION("main type disagrees with the root record")
    {
        auto document = wave_document(4, "AudioStreamWAV", raw, 1, 8000, false);
        document.main_type = "Resource";
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document))), audio_error);
    }
}

TEST_CASE("godot audio: an unknown root class resolves to no extension", "[godot][audio]")
{
    const binary_resource known =
        read(build_resource(text_document("AudioStreamWAV", {0x00, 0x11, 0x22, 0x33, 0x44, 0x55})));
    REQUIRE(known.records.back().type == "AudioStreamWAV");

    const binary_resource unknown = read(build_resource(text_document("TextureRect", {0x00})));
    REQUIRE(!(extension_of(unknown).has_value()));
    REQUIRE_THROWS_AS(collect(unknown), audio_error);
}

TEST_CASE("godot audio: a document with no records resolves to no extension", "[godot][audio]")
{
    resource_document_spec spec;
    spec.engine_major = 4;
    spec.engine_minor = 5;
    spec.format_version = 6;
    spec.main_type = "Resource";
    const binary_resource resource = read(build_resource(spec));
    REQUIRE(resource.records.empty());
    REQUIRE(!(extension_of(resource).has_value()));
}

TEST_CASE("godot audio: payload budgets are enforced", "[godot][audio]")
{
    const godot_resource_bytes pcm16 = {0x01, 0x00, 0x02, 0x00, 0x03, 0x00, 0x04, 0x00};
    SECTION("input byte budget")
    {
        const binary_resource resource =
            read(build_resource(wave_document(4, "AudioStreamWAV", pcm16, 1, 8000, false)));
        audio_limits limits;
        limits.max_input_bytes = 4;
        REQUIRE_THROWS_AS(extension_of(resource, limits), audio_error);
    }
    SECTION("8-bit frame budget")
    {
        const binary_resource resource =
            read(build_resource(wave_document(4, "AudioStreamWAV", pcm16, 0, 8000, false)));
        audio_limits limits;
        limits.max_pcm_frames = 1;
        REQUIRE_THROWS_AS(extension_of(resource, limits), audio_error);
    }
    SECTION("8-bit output budget")
    {
        const binary_resource resource =
            read(build_resource(wave_document(4, "AudioStreamWAV", pcm16, 0, 8000, false)));
        audio_limits limits;
        limits.max_output_bytes = 8;
        REQUIRE_THROWS_AS(extension_of(resource, limits), audio_error);
    }
    SECTION("16-bit frame budget")
    {
        const binary_resource resource =
            read(build_resource(wave_document(4, "AudioStreamWAV", pcm16, 1, 8000, false)));
        audio_limits limits;
        limits.max_pcm_frames = 1;
        REQUIRE_THROWS_AS(extension_of(resource, limits), audio_error);
    }
    SECTION("16-bit output budget")
    {
        const binary_resource resource =
            read(build_resource(wave_document(4, "AudioStreamWAV", pcm16, 1, 8000, false)));
        audio_limits limits;
        limits.max_output_bytes = 4;
        REQUIRE_THROWS_AS(extension_of(resource, limits), audio_error);
    }
}

TEST_CASE("godot audio: QOA descriptor validation rejects inconsistent headers", "[godot][audio]")
{
    const std::vector<std::int16_t> pcm = {0, 100, 200, 300};
    const std::vector<std::byte> valid = make_qoa(1, 8000, pcm);
    const auto document_for = [&](const std::vector<std::uint8_t> &raw) {
        return wave_document(4, "AudioStreamWAV", as_resource(raw), 3, 8000, false);
    };
    const auto bytes_of = [](const std::vector<std::byte> &raw) {
        std::vector<std::uint8_t> out;
        out.reserve(raw.size());
        for (const std::byte value : raw) {
            out.push_back(static_cast<std::uint8_t>(value));
        }
        return out;
    };

    SECTION("valid QOA resolves to WAVE")
    {
        const binary_resource resource = read(build_resource(document_for(bytes_of(valid))));
        REQUIRE(extension_of(resource) == std::optional<std::string_view>("wav"));
    }
    SECTION("missing capture pattern")
    {
        std::vector<std::uint8_t> raw = bytes_of(valid);
        raw[0] = 'x';
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document_for(raw)))), audio_error);
    }
    SECTION("shorter than the capture pattern")
    {
        const std::vector<std::uint8_t> raw = {'q'};
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document_for(raw)))), audio_error);
    }
    SECTION("shorter than a header and first frame")
    {
        std::vector<std::uint8_t> raw = bytes_of(valid);
        raw.resize(10);
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document_for(raw)))), audio_error);
    }
    SECTION("zero declared samples")
    {
        std::vector<std::uint8_t> raw = bytes_of(valid);
        raw[4] = raw[5] = raw[6] = raw[7] = 0;
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document_for(raw)))), audio_error);
    }
    SECTION("frame channel count mismatch")
    {
        std::vector<std::uint8_t> raw = bytes_of(valid);
        raw[8] = 2;
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document_for(raw)))), audio_error);
    }
    SECTION("frame sample rate mismatch")
    {
        std::vector<std::uint8_t> raw = bytes_of(valid);
        raw[9] = static_cast<std::uint8_t>(raw[9] ^ 0xFFU);
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document_for(raw)))), audio_error);
    }
    SECTION("frame declares zero samples")
    {
        std::vector<std::uint8_t> raw = bytes_of(valid);
        raw[12] = raw[13] = 0;
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document_for(raw)))), audio_error);
    }
    SECTION("frame declares more samples than the file header")
    {
        std::vector<std::uint8_t> raw = bytes_of(valid);
        raw[12] = 0xFF;
        raw[13] = 0xFF;
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document_for(raw)))), audio_error);
    }
    SECTION("frame smaller than its state header")
    {
        std::vector<std::uint8_t> raw = bytes_of(valid);
        raw[14] = 0;
        raw[15] = 1;
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document_for(raw)))), audio_error);
    }
    SECTION("frame larger than the payload")
    {
        std::vector<std::uint8_t> raw = bytes_of(valid);
        raw[14] = 0xFF;
        raw[15] = 0xFF;
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document_for(raw)))), audio_error);
    }
}

TEST_CASE("godot audio: MP3 pass-through preserves the exact bitstream", "[godot][audio]")
{
    const std::vector<std::uint8_t> valid_mp3_frame = complete_mp3_frame();
    SECTION("bare frame")
    {
        const binary_resource resource = read(build_resource(text_document("AudioStreamMP3", valid_mp3_frame)));
        REQUIRE(extension_of(resource) == std::optional<std::string_view>("mp3"));
        const std::vector<std::byte> output = collect(resource);
        REQUIRE(output.size() == valid_mp3_frame.size());
        REQUIRE(std::memcmp(output.data(), valid_mp3_frame.data(), valid_mp3_frame.size()) == 0);
    }
    SECTION("ID3v2 tag")
    {
        std::vector<std::uint8_t> data = {'I', 'D', '3', 4, 0, 0, 0, 0, 0, 0};
        data.insert(data.end(), valid_mp3_frame.begin(), valid_mp3_frame.end());
        const binary_resource resource = read(build_resource(text_document("AudioStreamMP3", data)));
        REQUIRE(extension_of(resource) == std::optional<std::string_view>("mp3"));
    }
    SECTION("ID3v2 tag with a footer")
    {
        std::vector<std::uint8_t> data = {'I', 'D', '3', 4, 0, 0x10, 0, 0, 0, 0};
        data.resize(20, 0);
        data.insert(data.end(), valid_mp3_frame.begin(), valid_mp3_frame.end());
        const binary_resource resource = read(build_resource(text_document("AudioStreamMP3", data)));
        REQUIRE(extension_of(resource) == std::optional<std::string_view>("mp3"));
    }
}

TEST_CASE("godot audio: unrecognizable MP3 payloads are rejected", "[godot][audio]")
{
    const auto reject = [](const std::vector<std::uint8_t> &data) {
        REQUIRE_THROWS_AS(extension_of(read(build_resource(text_document("AudioStreamMP3", data)))), audio_error);
    };
    SECTION("empty")
    {
        reject({});
    }
    SECTION("ID3 size byte with the high bit set")
    {
        reject({'I', 'D', '3', 4, 0, 0, 0x80, 0, 0, 0, 0, 0, 0, 0, 0});
    }
    SECTION("ID3 tag larger than the payload")
    {
        reject({'I', 'D', '3', 4, 0, 0, 0x7F, 0x7F, 0x7F, 0x7F, 0, 0});
    }
    SECTION("ID3 tag leaving too few bytes for a frame")
    {
        reject({'I', 'D', '3', 4, 0, 0, 0, 0, 0, 0, 0, 0});
    }
    SECTION("no sync byte")
    {
        reject({0x00, 0x00, 0x00, 0x00});
    }
    SECTION("sync mask missing")
    {
        reject({0xFF, 0x00, 0x00, 0x00});
    }
    SECTION("reserved version")
    {
        reject({0xFF, 0xE9, 0x90, 0x00});
    }
    SECTION("reserved layer")
    {
        reject({0xFF, 0xF9, 0x90, 0x00});
    }
    SECTION("reserved bitrate")
    {
        reject({0xFF, 0xFB, 0xF0, 0x00});
    }
    SECTION("reserved sample rate")
    {
        reject({0xFF, 0xFB, 0x9C, 0x00});
    }
}

TEST_CASE("godot audio: godot 3 Ogg payload passes through", "[godot][audio]")
{
    const std::vector<std::uint8_t> data = {'O', 'g', 'g', 'S', 0, 0, 0, 0};
    const binary_resource resource = read(build_resource(text_document("AudioStreamOGGVorbis", data)));
    REQUIRE(extension_of(resource) == std::optional<std::string_view>("ogg"));
    const std::vector<std::byte> output = collect(resource);
    REQUIRE(output.size() == data.size());
    REQUIRE(std::memcmp(output.data(), data.data(), data.size()) == 0);
}

TEST_CASE("godot audio: unusable Godot Ogg byte payloads resolve to nothing", "[godot][audio]")
{
    SECTION("no data payload (the Godot 2 filename form)")
    {
        resource_document_spec spec;
        spec.engine_major = 2;
        spec.engine_minor = 1;
        spec.format_version = 1;
        spec.main_type = "AudioStreamOGGVorbis";
        spec.string_table = {"file"};
        resource_record_spec record;
        record.path = "";
        record.type = "AudioStreamOGGVorbis";
        record.properties = {prop("file", string_value("res://track.ogg"))};
        spec.records = {record};
        const binary_resource resource = read(build_resource(spec));
        REQUIRE(!(extension_of(resource).has_value()));
        REQUIRE_THROWS_AS(collect(resource), audio_error);
    }
    SECTION("data payload with the wrong type")
    {
        auto document = text_document("AudioStreamOGGVorbis", {});
        document.records[0].properties[0].value = int_value(1);
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document))), audio_error);
    }
    SECTION("missing capture pattern")
    {
        const std::vector<std::uint8_t> data = {'X', 'g', 'g', 'S', 0};
        REQUIRE_THROWS_AS(extension_of(read(build_resource(text_document("AudioStreamOGGVorbis", data)))), audio_error);
    }
    SECTION("shorter than the capture header")
    {
        const std::vector<std::uint8_t> data = {'O', 'g', 'g', 'S'};
        REQUIRE_THROWS_AS(extension_of(read(build_resource(text_document("AudioStreamOGGVorbis", data)))), audio_error);
    }
    SECTION("unsupported bitstream version")
    {
        const std::vector<std::uint8_t> data = {'O', 'g', 'g', 'S', 2};
        REQUIRE_THROWS_AS(extension_of(read(build_resource(text_document("AudioStreamOGGVorbis", data)))), audio_error);
    }
}

TEST_CASE("godot audio: a Godot 4 OggPacketSequence remuxes to an Ogg stream", "[godot][audio]")
{
    const std::vector<std::uint8_t> ident = vorbis_identification(1, 44100);
    const std::vector<std::uint8_t> comment = vorbis_comment();
    const std::vector<std::uint8_t> setup = vorbis_setup();
    const std::vector<std::uint8_t> audio = {0xAB, 0xCD, 0xEF};
    const auto document = ogg_document({{ident}, {comment, setup}, {audio, audio}}, {0, 1000, 2000}, 44100.0);
    const binary_resource resource = read(build_resource(document));

    REQUIRE(extension_of(resource) == std::optional<std::string_view>("ogg"));
    const std::vector<std::byte> output = collect(resource);
    const parsed_ogg ogg = parse_ogg(output);
    REQUIRE(ogg.first_page_bos);
    REQUIRE(ogg.granules.back() == 2000);
    REQUIRE(contains(output, ident));
    REQUIRE(contains(output, setup));
    REQUIRE(contains(output, audio));
}

TEST_CASE("godot audio: the packet-sequence reference must be an internal matching record", "[godot][audio]")
{
    const std::vector<std::uint8_t> ident = vorbis_identification(1, 44100);
    const std::vector<std::uint8_t> comment = vorbis_comment();
    const std::vector<std::uint8_t> setup = vorbis_setup();
    const auto pages = std::vector<std::vector<std::vector<std::uint8_t>>>{{ident, comment, setup}};

    SECTION("missing packet_sequence field")
    {
        resource_document_spec spec;
        spec.engine_major = 4;
        spec.engine_minor = 5;
        spec.format_version = 6;
        spec.named_scene_ids = true;
        spec.main_type = "AudioStreamOggVorbis";
        resource_record_spec root;
        root.path = "";
        root.type = "AudioStreamOggVorbis";
        root.properties = {prop("loop", bool_value(false))};
        spec.records = {root};
        REQUIRE_THROWS_AS(extension_of(read(build_resource(spec))), audio_error);
    }
    SECTION("packet_sequence with the wrong type")
    {
        const auto document = ogg_document(pages, {0}, 44100.0, int_value(5));
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document))), audio_error);
    }
    SECTION("null reference")
    {
        const auto document = ogg_document(pages, {0}, 44100.0, null_reference_value());
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document))), audio_error);
    }
    SECTION("external reference")
    {
        const auto document = ogg_document(pages, {0}, 44100.0, external_reference_value());
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document))), audio_error);
    }
    SECTION("reference to the wrong class")
    {
        const auto document = ogg_document(pages, {0}, 44100.0, reference_value(0));
        auto broken = document;
        broken.records[0].type = "Resource";
        REQUIRE_THROWS_AS(extension_of(read(build_resource(broken))), audio_error);
    }
}

TEST_CASE("godot audio: malformed OggPacketSequence descriptors are rejected", "[godot][audio]")
{
    const std::vector<std::uint8_t> ident = vorbis_identification(1, 44100);
    const std::vector<std::uint8_t> comment = vorbis_comment();
    const std::vector<std::uint8_t> setup = vorbis_setup();
    const godot_resource_bytes valid_pages = single_page({ident, comment, setup});
    const godot_resource_bytes valid_granules = int64_array_value(std::vector<std::int64_t>{0});
    const godot_resource_bytes valid_rate = real_value(44100.0);

    const auto reject = [&](const resource_document_spec &document) {
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document))), audio_error);
    };

    SECTION("non-finite sample rate")
    {
        reject(ogg_from_values(valid_pages, valid_granules, real_value(std::nan(""))));
    }
    SECTION("negative sample rate")
    {
        reject(ogg_from_values(valid_pages, valid_granules, real_value(-1.0)));
    }
    SECTION("out-of-range sample rate")
    {
        reject(ogg_from_values(valid_pages, valid_granules, real_value(200000000.0)));
    }
    SECTION("sample rate is not a real")
    {
        reject(ogg_from_values(valid_pages, valid_granules, int_value(44100)));
    }
    SECTION("packet_data is not an array")
    {
        reject(ogg_from_values(bytes_value(ident), valid_granules, valid_rate));
    }
    SECTION("no pages")
    {
        reject(ogg_from_values(array_value({}), valid_granules, valid_rate));
    }
    SECTION("granule list is not an integer array")
    {
        reject(ogg_from_values(valid_pages, int_value(0), valid_rate));
    }
    SECTION("granule count does not match the page count")
    {
        reject(ogg_from_values(valid_pages, int64_array_value(std::vector<std::int64_t>{0, 1}), valid_rate));
    }
    SECTION("page is not an array")
    {
        reject(ogg_from_values(array_value({int_value(5)}), valid_granules, valid_rate));
    }
    SECTION("empty page")
    {
        reject(ogg_from_values(array_value({array_value({})}), valid_granules, valid_rate));
    }
    SECTION("granule below -1")
    {
        reject(ogg_from_values(valid_pages, int64_array_value(std::vector<std::int64_t>{-2}), valid_rate));
    }
    SECTION("non-monotonic granules")
    {
        const godot_resource_bytes pages =
            array_value({array_value({bytes_value(ident)}), array_value({bytes_value(comment)}),
                         array_value({bytes_value(setup)})});
        reject(ogg_from_values(pages, int64_array_value(std::vector<std::int64_t>{0, 5000, 1000}), valid_rate));
    }
    SECTION("packet is not bytes")
    {
        reject(ogg_from_values(array_value({array_value({int_value(1), bytes_value(ident), bytes_value(setup)})}),
                               valid_granules, valid_rate));
    }
    SECTION("fewer than three packets")
    {
        reject(ogg_from_values(single_page({ident, comment}), valid_granules, valid_rate));
    }
    SECTION("input byte budget")
    {
        audio_limits limits;
        limits.max_input_bytes = 4;
        REQUIRE_THROWS_AS(
            extension_of(read(build_resource(ogg_from_values(valid_pages, valid_granules, valid_rate))), limits),
            audio_error);
    }
}

TEST_CASE("godot audio: Vorbis header validation rejects inconsistent prefixes", "[godot][audio]")
{
    const std::vector<std::uint8_t> comment = vorbis_comment();
    const std::vector<std::uint8_t> setup = vorbis_setup();
    const auto with_first = [&](const std::vector<std::uint8_t> &first, const double rate) {
        return ogg_from_values(single_page({first, comment, setup}), int64_array_value(std::vector<std::int64_t>{0}),
                               real_value(rate));
    };
    const auto reject = [&](const resource_document_spec &document) {
        REQUIRE_THROWS_AS(extension_of(read(build_resource(document))), audio_error);
    };

    SECTION("truncated header packet")
    {
        reject(with_first({1, 2}, 44100.0));
    }
    SECTION("wrong packet type byte")
    {
        std::vector<std::uint8_t> first = vorbis_identification(1, 44100);
        first[0] = 2;
        reject(with_first(first, 44100.0));
    }
    SECTION("missing vorbis signature")
    {
        std::vector<std::uint8_t> first = vorbis_identification(1, 44100);
        first[1] = 'x';
        reject(with_first(first, 44100.0));
    }
    SECTION("truncated identification header")
    {
        std::vector<std::uint8_t> first = vorbis_identification(1, 44100);
        first.resize(10);
        reject(with_first(first, 44100.0));
    }
    SECTION("unsupported identification version")
    {
        reject(with_first(vorbis_identification(1, 44100, 1), 44100.0));
    }
    SECTION("zero channels")
    {
        reject(with_first(vorbis_identification(0, 44100), 44100.0));
    }
    SECTION("too many channels")
    {
        reject(with_first(vorbis_identification(3, 44100), 44100.0));
    }
    SECTION("zero identification sample rate")
    {
        reject(with_first(vorbis_identification(1, 0), 44100.0));
    }
    SECTION("identification sample rate disagrees with the declared rate")
    {
        reject(with_first(vorbis_identification(1, 48000), 44100.0));
    }
    SECTION("missing framing flag")
    {
        reject(with_first(vorbis_identification(1, 44100, 0, 0), 44100.0));
    }
}

TEST_CASE("godot audio: a sink exception propagates unchanged", "[godot][audio]")
{
    const std::vector<std::uint8_t> valid_mp3_frame = complete_mp3_frame();
    const auto throwing_sink = [](const std::span<const std::byte>) { throw std::runtime_error("sink boom"); };
    SECTION("WAVE output")
    {
        const binary_resource resource =
            read(build_resource(wave_document(4, "AudioStreamWAV", {0x01, 0x00, 0x02, 0x00}, 1, 8000, false)));
        REQUIRE_THROWS_AS(write_audio_resource(resource, throwing_sink), std::runtime_error);
    }
    SECTION("MP3 pass-through")
    {
        const binary_resource resource = read(build_resource(text_document("AudioStreamMP3", valid_mp3_frame)));
        REQUIRE_THROWS_AS(write_audio_resource(resource, throwing_sink), std::runtime_error);
    }
    SECTION("Ogg byte pass-through")
    {
        const binary_resource resource =
            read(build_resource(text_document("AudioStreamOGGVorbis", {'O', 'g', 'g', 'S', 0, 0})));
        REQUIRE_THROWS_AS(write_audio_resource(resource, throwing_sink), std::runtime_error);
    }
    SECTION("Ogg remux")
    {
        const auto document =
            ogg_document({{vorbis_identification(1, 44100), vorbis_comment(), vorbis_setup()}}, {0}, 44100.0);
        const binary_resource resource = read(build_resource(document));
        REQUIRE_THROWS_AS(write_audio_resource(resource, throwing_sink), std::runtime_error);
    }
}

// ---------------------------------------------------------------------------------------------
// Regression coverage for the seven consolidated audio-conversion findings. Each case states the
// observable requirement the converter must satisfy; the body records the real outcome against the
// current production so the required fix has a failing test to drive it.
// ---------------------------------------------------------------------------------------------

TEST_CASE("godot audio: finding 1 - embedded MP3 and Ogg pass-through honour max_output_bytes",
          "[godot][audio][finding1]")
{
    const std::vector<std::uint8_t> frame = complete_mp3_frame();
    const std::vector<std::uint8_t> ogg_bytes = {'O', 'g', 'g', 'S', 0, 0, 0, 0};
    const auto mp3_resource = [&frame] { return read(build_resource(text_document("AudioStreamMP3", frame))); };
    const auto ogg_resource = [&ogg_bytes] {
        return read(build_resource(text_document("AudioStreamOGGVorbis", ogg_bytes)));
    };

    SECTION("an exact MP3 output budget still passes the bitstream through")
    {
        const binary_resource resource = mp3_resource();
        audio_limits limits;
        limits.max_output_bytes = frame.size();
        REQUIRE(extension_of(resource, limits) == std::optional<std::string_view>("mp3"));
        REQUIRE(collect(resource, limits).size() == frame.size());
    }
    SECTION("an MP3 payload one byte over the budget is rejected before the sink")
    {
        const binary_resource resource = mp3_resource();
        audio_limits limits;
        limits.max_output_bytes = frame.size() - 1;
        REQUIRE_THROWS_AS(extension_of(resource, limits), audio_error);
        REQUIRE_THROWS_AS(collect(resource, limits), audio_error);
    }
    SECTION("a zero output budget rejects a non-empty MP3 payload")
    {
        const binary_resource resource = mp3_resource();
        audio_limits limits;
        limits.max_output_bytes = 0;
        REQUIRE_THROWS_AS(extension_of(resource, limits), audio_error);
        REQUIRE_THROWS_AS(collect(resource, limits), audio_error);
    }
    SECTION("an exact Ogg output budget still passes the stream through")
    {
        const binary_resource resource = ogg_resource();
        audio_limits limits;
        limits.max_output_bytes = ogg_bytes.size();
        REQUIRE(extension_of(resource, limits) == std::optional<std::string_view>("ogg"));
        REQUIRE(collect(resource, limits).size() == ogg_bytes.size());
    }
    SECTION("an Ogg byte payload one byte over the budget is rejected before the sink")
    {
        const binary_resource resource = ogg_resource();
        audio_limits limits;
        limits.max_output_bytes = ogg_bytes.size() - 1;
        REQUIRE_THROWS_AS(extension_of(resource, limits), audio_error);
        REQUIRE_THROWS_AS(collect(resource, limits), audio_error);
    }
}

TEST_CASE("godot audio: finding 2 - every PCM geometry preflights frames and the full WAVE size",
          "[godot][audio][finding2]")
{
    // Four stereo PCM16 frames: sixteen bytes, matching the declared length below.
    const godot_resource_bytes stereo16 = {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
                                           0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00};
    const auto sample16_resource = [&stereo16] {
        return read(build_resource(sample_document(stereo16, "raw", "pcm16", true, 4, 8000)));
    };
    const godot_resource_bytes mono8 = {0x00, 0x11, 0x22, 0x33};
    const auto sample8_resource = [&mono8] {
        return read(build_resource(sample_document(mono8, "raw", "pcm8", false, 4, 8000)));
    };

    SECTION("Godot 2 Sample PCM frame budget is enforced before allocation")
    {
        const binary_resource resource = sample16_resource();
        audio_limits limits;
        limits.max_pcm_frames = 3;
        REQUIRE_THROWS_AS(extension_of(resource, limits), audio_error);
    }
    SECTION("Godot 2 Sample PCM8 frame budget is enforced before allocation")
    {
        const binary_resource resource = sample8_resource();
        audio_limits limits;
        limits.max_pcm_frames = 3;
        REQUIRE_THROWS_AS(extension_of(resource, limits), audio_error);
    }
    SECTION("the exact canonical 44-byte WAVE size is accepted")
    {
        const binary_resource resource = sample16_resource();
        audio_limits limits;
        limits.max_output_bytes = 44 + stereo16.size();
        REQUIRE(extension_of(resource, limits) == std::optional<std::string_view>("wav"));
    }
    SECTION("a payload that fits but whose 44-byte header does not is rejected")
    {
        const binary_resource resource = sample16_resource();
        audio_limits limits;
        limits.max_output_bytes = 44 + stereo16.size() - 1;
        REQUIRE_THROWS_AS(extension_of(resource, limits), audio_error);
    }
    SECTION("a budget equal to the payload alone omits the header uncovered")
    {
        const binary_resource resource = sample16_resource();
        audio_limits limits;
        limits.max_output_bytes = stereo16.size();
        REQUIRE_THROWS_AS(extension_of(resource, limits), audio_error);
    }
    SECTION("Godot 4 PCM16 framing also accounts for the 44-byte header")
    {
        const godot_resource_bytes raw = {0x01, 0x00, 0x02, 0x00};
        const binary_resource resource = read(build_resource(wave_document(4, "AudioStreamWAV", raw, 1, 8000, false)));
        audio_limits limits;
        limits.max_output_bytes = 44 + raw.size() - 1;
        REQUIRE_THROWS_AS(extension_of(resource, limits), audio_error);
    }
}

TEST_CASE("godot audio: finding 3 - Godot 2 Sample IMA validates the declared frame length", "[godot][audio][finding3]")
{
    const std::vector<std::uint8_t> packed = {0x01, 0x23, 0x45};
    const godot_resource_bytes raw = to_resource(make_ima_mono(packed));
    const auto resource_for = [&raw](const std::int64_t length) {
        return read(build_resource(sample_document(raw, "raw", "ima_adpcm", false, length, 44100)));
    };

    SECTION("a declared length equal to the encoded capacity is accepted")
    {
        const binary_resource resource = resource_for(6);
        REQUIRE(extension_of(resource) == std::optional<std::string_view>("wav"));
        REQUIRE(parse_wave(collect(resource)).samples == ima_reference_pcm(packed, 1));
    }
    SECTION("a declared length beyond the encoded capacity is rejected")
    {
        REQUIRE_THROWS_AS(extension_of(resource_for(7)), audio_error);
        REQUIRE_THROWS_AS(collect(resource_for(7)), audio_error);
    }
    SECTION("a non-empty payload with a zero declared length is rejected")
    {
        REQUIRE_THROWS_AS(extension_of(resource_for(0)), audio_error);
    }
    SECTION("a declared length far below the encoded capacity is rejected")
    {
        REQUIRE_THROWS_AS(extension_of(resource_for(2)), audio_error);
    }
    SECTION("an odd declared frame count trims the padding nibble")
    {
        const binary_resource resource = resource_for(5);
        const std::vector<std::int16_t> expected = ima_reference_pcm(packed, 1);
        const parsed_wave wave = parse_wave(collect(resource));
        REQUIRE(wave.samples.size() == 5);
        REQUIRE(wave.samples == std::vector<std::int16_t>(expected.begin(), expected.begin() + 5));
    }
}

TEST_CASE("godot audio: Godot 2 Sample IMA accepts the writer's four seed bytes per lane",
          "[godot][audio][sample-padding]")
{
    const std::vector<std::uint8_t> packed = {0x12, 0x34, 0x56, 0x78, 0x9A, 0xBC};
    godot_resource_bytes mono(4, 0);
    mono.insert(mono.end(), packed.begin(), packed.end());
    const binary_resource even = read(build_resource(sample_document(mono, "raw", "ima_adpcm", false, 12, 44100)));
    REQUIRE(extension_of(even) == std::optional<std::string_view>("wav"));
    REQUIRE(parse_wave(collect(even)).samples == ima_reference_pcm(packed, 1));

    const binary_resource odd = read(build_resource(sample_document(mono, "raw", "ima_adpcm", false, 11, 44100)));
    REQUIRE(extension_of(odd) == std::optional<std::string_view>("wav"));
    const auto expected = ima_reference_pcm(packed, 1);
    REQUIRE(parse_wave(collect(odd)).samples == std::vector<std::int16_t>(expected.begin(), expected.begin() + 11));

    godot_resource_bytes stereo;
    godot_resource_bytes paired_data;
    for (const std::uint8_t value : packed) {
        paired_data.push_back(value);
        paired_data.push_back(value);
    }
    for (const std::uint8_t value : mono) {
        stereo.push_back(value);
        stereo.push_back(value);
    }
    const binary_resource paired = read(build_resource(sample_document(stereo, "raw", "ima_adpcm", true, 12, 44100)));
    REQUIRE(extension_of(paired) == std::optional<std::string_view>("wav"));
    REQUIRE(parse_wave(collect(paired)).samples == ima_reference_pcm(paired_data, 2));

    mono[1] = 0x7F;
    const binary_resource invalid_seed =
        read(build_resource(sample_document(mono, "raw", "ima_adpcm", false, 12, 44100)));
    REQUIRE_THROWS_AS(extension_of(invalid_seed), audio_error);
    REQUIRE_THROWS_AS(collect(invalid_seed), audio_error);
}

TEST_CASE("godot audio: default metadata probe accepts PCM beyond the old frame ceiling",
          "[godot][audio][large-default]")
{
    constexpr std::size_t frames = std::size_t{32} * 1024 * 1024 + 1;
    binary_resource resource;
    resource.engine.major = 4;
    resource.main_type = "AudioStreamWAV";
    resource.records.emplace_back();
    resource.records.back().type = resource.main_type;
    unpackrat::godot::resource_value payload;
    payload.kind = unpackrat::godot::value_kind::bytes;
    payload.bytes.resize(frames, std::byte{0});
    resource.records.back().properties.push_back({"data", std::move(payload)});

    const auto info = unpackrat::godot::inspect_audio_resource(resource);
    REQUIRE(info.has_value());
    if (!info.has_value()) {
        return;
    }
    REQUIRE(info.value().extension == "wav");
    REQUIRE(info.value().output_size == std::optional<std::uint64_t>(44 + 2 * frames));
}

TEST_CASE("godot audio: metadata inspection uses declared lengths and captured prefixes",
          "[godot][audio][audio-metadata]")
{
    SECTION("a declared IMA length beyond the platform input budget is rejected before frame arithmetic")
    {
        binary_resource wave = read(build_resource(wave_document(4, "AudioStreamWAV", {0, 0}, 2, 8000, false)));
        wave.records.back().properties.front().value.byte_length = (std::numeric_limits<std::uint64_t>::max)();
        CHECK_THROWS_AS(unpackrat::godot::inspect_audio_resource_metadata(wave), audio_error);

        binary_resource sample = read(build_resource(sample_document({0, 0}, "raw", "ima_adpcm", false, 2, 8000)));
        sample.records.back().properties[1].value.dictionary[4].value.byte_length =
            (std::numeric_limits<std::uint64_t>::max)();
        CHECK_THROWS_AS(unpackrat::godot::inspect_audio_resource_metadata(sample), audio_error);
    }
    SECTION("PCM8 uses the full declared frame count")
    {
        binary_resource resource =
            read(build_resource(wave_document(4, "AudioStreamWAV", {1, 2, 3, 4}, 0, 8000, false)));
        auto &data = resource.records.back().properties.front().value;
        data.byte_length = 100;
        const auto info = unpackrat::godot::inspect_audio_resource_metadata(resource);
        REQUIRE(info.has_value());
        if (!info.has_value()) {
            return;
        }
        REQUIRE(info.value().extension == "wav");
        REQUIRE(info.value().output_size == std::optional<std::uint64_t>(244));
    }
    SECTION("legacy Sample IMA uses the full nibble capacity and seed prefix")
    {
        const godot_resource_bytes payload = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0};
        binary_resource resource = read(build_resource(sample_document(payload, "raw", "ima_adpcm", false, 20, 8000)));
        auto &data = resource.records.back().properties[1].value.dictionary[4].value;
        data.byte_length = data.bytes.size();
        data.bytes.resize(8);
        const auto info = unpackrat::godot::inspect_audio_resource_metadata(resource);
        REQUIRE(info.has_value());
        if (!info.has_value()) {
            return;
        }
        REQUIRE(info.value().extension == "wav");
        REQUIRE(info.value().output_size == std::optional<std::uint64_t>(84));
    }
    SECTION("QOA uses its first frame header and declared stored size")
    {
        const std::vector<std::int16_t> samples = {1, 2, 3};
        const auto qoa = to_resource(make_qoa(1, 8000, samples));
        binary_resource resource = read(build_resource(wave_document(4, "AudioStreamWAV", qoa, 3, 8000, false)));
        auto &data = resource.records.back().properties.front().value;
        data.byte_length = data.bytes.size();
        data.bytes.resize(16);
        const auto info = unpackrat::godot::inspect_audio_resource_metadata(resource);
        REQUIRE(info.has_value());
        if (!info.has_value()) {
            return;
        }
        REQUIRE(info.value().output_size == std::optional<std::uint64_t>(50));
    }
    SECTION("Ogg and MP3 pass through declared stored lengths")
    {
        binary_resource ogg = read(build_resource(text_document("AudioStreamOGGVorbis", {'O', 'g', 'g', 'S', 0})));
        auto &ogg_data = ogg.records.back().properties.front().value;
        ogg_data.byte_length = 100;
        const auto ogg_info = unpackrat::godot::inspect_audio_resource_metadata(ogg);
        REQUIRE(ogg_info.has_value());
        if (!ogg_info.has_value()) {
            return;
        }
        REQUIRE(ogg_info.value().output_size == std::optional<std::uint64_t>(100));

        const godot_resource_bytes mp3 = to_resource(test::support::make_mp3_silence(1, false));
        binary_resource stream = read(build_resource(text_document("AudioStreamMP3", mp3)));
        auto &mp3_data = stream.records.back().properties.front().value;
        mp3_data.byte_length = mp3_data.bytes.size();
        mp3_data.bytes.resize(4);
        const auto info = unpackrat::godot::inspect_audio_resource_metadata(stream);
        REQUIRE(info.has_value());
        if (!info.has_value()) {
            return;
        }
        REQUIRE(info.value().extension == "mp3");
        REQUIRE(info.value().output_size == std::optional<std::uint64_t>(mp3.size()));
    }
    SECTION("a declared length cannot be shorter than the captured prefix")
    {
        binary_resource resource =
            read(build_resource(wave_document(4, "AudioStreamWAV", {1, 2, 3, 4}, 0, 8000, false)));
        resource.records.back().properties.front().value.byte_length = 3;
        REQUIRE_THROWS_AS(unpackrat::godot::inspect_audio_resource_metadata(resource), audio_error);
    }
    SECTION("a declared PCM length still honors an explicit caller budget")
    {
        binary_resource resource =
            read(build_resource(wave_document(4, "AudioStreamWAV", {1, 2, 3, 4}, 0, 8000, false)));
        resource.records.back().properties.front().value.byte_length = 100;
        audio_limits input_tight;
        input_tight.max_input_bytes = 99;
        REQUIRE_THROWS_AS(unpackrat::godot::inspect_audio_resource_metadata(resource, input_tight), audio_error);
        audio_limits output_tight;
        output_tight.max_output_bytes = 243;
        REQUIRE_THROWS_AS(unpackrat::godot::inspect_audio_resource_metadata(resource, output_tight), audio_error);
    }
    SECTION("a truncated or unrecognized MP3 prefix is not advertised")
    {
        const godot_resource_bytes mp3 = to_resource(test::support::make_mp3_silence(1, false));
        binary_resource resource = read(build_resource(text_document("AudioStreamMP3", mp3)));
        auto &data = resource.records.back().properties.front().value;
        data.byte_length = data.bytes.size();
        data.bytes.resize(3);
        REQUIRE_THROWS_AS(unpackrat::godot::inspect_audio_resource_metadata(resource), audio_error);
        data.bytes.push_back(std::byte{0});
        data.bytes.front() = std::byte{0};
        REQUIRE_THROWS_AS(unpackrat::godot::inspect_audio_resource_metadata(resource), audio_error);
    }
    SECTION("an ID3 prefix is recognized before its declared embedded frame")
    {
        const godot_resource_bytes id3 = {'I', 'D', '3', 4, 0, 0, 0, 0, 0, 0};
        binary_resource resource = read(build_resource(text_document("AudioStreamMP3", id3)));
        resource.records.back().properties.front().value.byte_length = 100;
        const auto info = unpackrat::godot::inspect_audio_resource_metadata(resource);
        REQUIRE(info.has_value());
        if (!info.has_value()) {
            return;
        }
        REQUIRE(info.value().output_size == std::optional<std::uint64_t>(100));
    }
    SECTION("a short ID3 tag does not substitute for a captured frame")
    {
        const godot_resource_bytes id3 = {'I', 'D', '3', 4, 0, 0, 0, 0, 0};
        const binary_resource resource = read(build_resource(text_document("AudioStreamMP3", id3)));
        REQUIRE_THROWS_AS(unpackrat::godot::inspect_audio_resource_metadata(resource), audio_error);
    }
    SECTION("an unset declared length uses the captured PCM payload")
    {
        binary_resource resource =
            read(build_resource(wave_document(4, "AudioStreamWAV", {1, 2, 3, 4}, 0, 8000, false)));
        resource.records.back().properties.front().value.byte_length = 0;
        const auto info = unpackrat::godot::inspect_audio_resource_metadata(resource);
        REQUIRE(info.has_value());
        if (!info.has_value()) {
            return;
        }
        REQUIRE(info.value().output_size == std::optional<std::uint64_t>(52));
    }
    SECTION("IMA frame arithmetic rejects a declared byte count above half of uint64")
    {
        binary_resource modern = read(build_resource(wave_document(4, "AudioStreamWAV", {0}, 2, 8000, false)));
        modern.records.back().properties.front().value.byte_length =
            (std::numeric_limits<std::uint64_t>::max)() / 2 + 1;
        REQUIRE_THROWS_AS(unpackrat::godot::inspect_audio_resource_metadata(modern), audio_error);

        binary_resource legacy =
            read(build_resource(sample_document({0, 0, 0, 0, 0}, "raw", "ima_adpcm", false, 1, 8000)));
        legacy.records.back().properties[1].value.dictionary[4].value.byte_length =
            (std::numeric_limits<std::uint64_t>::max)() / 2 + 1;
        REQUIRE_THROWS_AS(unpackrat::godot::inspect_audio_resource_metadata(legacy), audio_error);
    }
    SECTION("a Sample IMA seed must fit in the captured prefix")
    {
        const godot_resource_bytes payload(14, 0);
        binary_resource resource = read(build_resource(sample_document(payload, "raw", "ima_adpcm", false, 20, 8000)));
        auto &data = resource.records.back().properties[1].value.dictionary[4].value;
        data.byte_length = data.bytes.size();
        data.bytes.resize(3);
        REQUIRE_THROWS_AS(unpackrat::godot::inspect_audio_resource_metadata(resource), audio_error);
    }
    SECTION("a packet sequence uses the declared packet lengths")
    {
        const auto ident = vorbis_identification(1, 44100);
        const auto comment = vorbis_comment();
        const auto setup = vorbis_setup();
        const auto document = ogg_document({{ident, comment, setup}}, {0}, 44100.0);
        binary_resource resource = read(build_resource(document));
        auto &packet = resource.records.front().properties.front().value.array.front().array.back();
        packet.byte_length = packet.bytes.size() + 100;
        const auto info = unpackrat::godot::inspect_audio_resource_metadata(resource);
        REQUIRE(info.has_value());
        if (!info.has_value()) {
            return;
        }
        REQUIRE(info.value().extension == "ogg");
        REQUIRE(!info.value().output_size.has_value());
        audio_limits tight;
        tight.max_input_bytes = ident.size() + comment.size() + setup.size() + 99;
        REQUIRE_THROWS_AS(unpackrat::godot::inspect_audio_resource_metadata(resource, tight), audio_error);
    }
}

TEST_CASE("godot audio: sparse PCM8 extraction streams beyond the old default frame ceiling",
          "[godot][audio][audio-streaming]")
{
    constexpr std::uint64_t frames = std::uint64_t{32} * 1024 * 1024 + 1;
    binary_resource resource;
    resource.engine.major = 4;
    resource.main_type = "AudioStreamWAV";
    resource.records.emplace_back();
    resource.records.back().type = resource.main_type;
    unpackrat::godot::resource_value payload;
    payload.kind = unpackrat::godot::value_kind::bytes;
    payload.byte_length = frames;
    payload.byte_offset = 0;
    resource.records.back().properties.push_back({"data", std::move(payload)});

    std::uint64_t read_bytes = 0;
    const unpackrat::io::byte_source source{
        frames,
        [&](const std::uint64_t offset, const std::span<std::byte> output) {
            REQUIRE(offset <= frames);
            REQUIRE(output.size() <= 4096);
            REQUIRE(output.size() <= frames - offset);
            std::fill(output.begin(), output.end(), std::byte{0});
            read_bytes += output.size();
        },
    };
    std::uint64_t written = 0;
    std::vector<std::byte> header;
    const unpackrat::io::byte_sink sink = [&](const std::span<const std::byte> chunk) {
        REQUIRE(chunk.size() <= 4096);
        if (header.size() < 44) {
            const std::size_t needed = 44 - header.size();
            const auto prefix = chunk.first((std::min)(needed, chunk.size()));
            header.insert(header.end(), prefix.begin(), prefix.end());
        }
        written += chunk.size();
    };
    unpackrat::godot::write_audio_resource_streaming(resource, source, sink);
    REQUIRE(read_bytes == frames);
    REQUIRE(written == 44 + frames * 2);
    REQUIRE(header.size() == 44);
    REQUIRE(std::memcmp(header.data(), "RIFF", 4) == 0);
    REQUIRE(std::memcmp(header.data() + 8, "WAVE", 4) == 0);
}

TEST_CASE("godot audio: metadata-only Sample IMA uses its declared byte length beyond prefix",
          "[godot][audio][sample-metadata-prefix]")
{
    const godot_resource_bytes payload(4 + 100, 0);
    const auto serialized = build_resource(sample_document(payload, "raw", "ima_adpcm", false, 200, 8000));
    const unpackrat::io::byte_source source{
        serialized.size(),
        [&](const std::uint64_t offset, const std::span<std::byte> output) {
            REQUIRE(offset <= serialized.size());
            REQUIRE(output.size() <= serialized.size() - offset);
            for (std::size_t i = 0; i < output.size(); ++i) {
                output[i] = std::byte{serialized[static_cast<std::size_t>(offset) + i]};
            }
        },
    };
    const binary_resource metadata = unpackrat::godot::read_binary_resource_metadata(source.size, source.read);
    const auto info = unpackrat::godot::inspect_audio_resource_metadata(metadata);
    REQUIRE(info.has_value());
    if (!info.has_value()) {
        return;
    }
    REQUIRE(info.value().extension == "wav");
    REQUIRE(info.value().output_size == 44 + 200 * 2);
    std::uint64_t written = 0;
    unpackrat::godot::write_audio_resource_streaming(
        metadata, source, [&](const std::span<const std::byte> chunk) { written += chunk.size(); });
    REQUIRE(written == 44 + 200 * 2);
}

TEST_CASE("godot audio: typed MP3 with two harmless leading bytes is advertised and preserved",
          "[godot][audio][mp3-leading-two]")
{
    std::vector<std::uint8_t> payload{0x89, 0x06};
    const auto frame = complete_mp3_frame();
    payload.insert(payload.end(), frame.begin(), frame.end());
    const auto serialized = build_resource(text_document("AudioStreamMP3", payload));
    const binary_resource full = read(serialized);
    REQUIRE(extension_of(full) == std::optional<std::string_view>("mp3"));
    const auto full_output = collect(full);
    REQUIRE(full_output ==
            std::vector<std::byte>(reinterpret_cast<const std::byte *>(payload.data()),
                                   reinterpret_cast<const std::byte *>(payload.data()) + payload.size()));
    const unpackrat::io::byte_source source{
        serialized.size(),
        [&](const std::uint64_t offset, const std::span<std::byte> output) {
            REQUIRE(offset <= serialized.size());
            REQUIRE(output.size() <= serialized.size() - offset);
            for (std::size_t i = 0; i < output.size(); ++i) {
                output[i] = std::byte{serialized[static_cast<std::size_t>(offset) + i]};
            }
        },
    };
    const binary_resource metadata = unpackrat::godot::read_binary_resource_metadata(source.size, source.read);
    const auto info = unpackrat::godot::inspect_audio_resource_metadata(metadata);
    REQUIRE(info.has_value());
    if (!info.has_value()) {
        return;
    }
    REQUIRE(info.value().extension == "mp3");
    std::vector<std::byte> actual;
    unpackrat::godot::write_audio_resource_streaming(metadata, source, [&](const std::span<const std::byte> chunk) {
        actual.insert(actual.end(), chunk.begin(), chunk.end());
    });
    REQUIRE(actual == std::vector<std::byte>(reinterpret_cast<const std::byte *>(payload.data()),
                                             reinterpret_cast<const std::byte *>(payload.data()) + payload.size()));
}

TEST_CASE("godot audio: metadata streaming validates each source extent before reading",
          "[godot][audio][audio-streaming-bounds]")
{
    const auto no_read = [](std::uint64_t, std::span<std::byte>) { FAIL("invalid extent reached source"); };
    const auto no_write = [](std::span<const std::byte>) { FAIL("invalid extent reached sink"); };
    const unpackrat::io::byte_source empty_source{0, no_read};

    SECTION("unsupported root")
    {
        const binary_resource unsupported{};
        REQUIRE_THROWS_AS(unpackrat::godot::write_audio_resource_streaming(unsupported, empty_source, no_write),
                          audio_error);
    }
    SECTION("a raw MP3 payload starts after the source")
    {
        binary_resource resource = read(build_resource(text_document("AudioStreamMP3", complete_mp3_frame())));
        resource.records.back().properties.front().value.byte_offset = 1;
        REQUIRE_THROWS_AS(unpackrat::godot::write_audio_resource_streaming(resource, empty_source, no_write),
                          audio_error);
    }
    SECTION("a raw MP3 payload extends past the source")
    {
        binary_resource resource = read(build_resource(text_document("AudioStreamMP3", complete_mp3_frame())));
        resource.records.back().properties.front().value.byte_offset = 0;
        REQUIRE_THROWS_AS(unpackrat::godot::write_audio_resource_streaming(resource, empty_source, no_write),
                          audio_error);
    }
    SECTION("an Ogg packet starts after the source")
    {
        const auto document =
            ogg_document({{vorbis_identification(1, 44100), vorbis_comment(), vorbis_setup()}}, {0}, 44100.0);
        binary_resource resource = read(build_resource(document));
        resource.records.front().properties.front().value.array.front().array.front().byte_offset = 1;
        REQUIRE_THROWS_AS(unpackrat::godot::write_audio_resource_streaming(resource, empty_source, no_write),
                          audio_error);
    }
    SECTION("an Ogg packet extends past the source")
    {
        const auto document =
            ogg_document({{vorbis_identification(1, 44100), vorbis_comment(), vorbis_setup()}}, {0}, 44100.0);
        binary_resource resource = read(build_resource(document));
        resource.records.front().properties.front().value.array.front().array.front().byte_offset = 0;
        REQUIRE_THROWS_AS(unpackrat::godot::write_audio_resource_streaming(resource, empty_source, no_write),
                          audio_error);
    }
}

TEST_CASE("godot audio: streamed QOA rejects damaged later frame headers", "[godot][audio][qoa-stream-bounds]")
{
    const std::vector<std::int16_t> pcm(6000, 0);
    const std::vector<std::byte> valid = make_qoa(1, 8000, pcm);
    const std::size_t second = 8 + (std::to_integer<unsigned>(valid[14]) << 8U) + std::to_integer<unsigned>(valid[15]);
    REQUIRE(second + 8 < valid.size());
    binary_resource metadata =
        read(build_resource(wave_document(4, "AudioStreamWAV", to_resource(valid), 3, 8000, false)));
    auto &data = metadata.records.back().properties.front().value;
    data.bytes.resize(16);
    data.byte_offset = 0;

    std::vector<std::byte> source_bytes = valid;
    SECTION("truncated second header")
    {
        source_bytes.resize(second + 4);
    }
    SECTION("second frame exceeds the bounded frame buffer")
    {
        source_bytes[second + 6] = std::byte{0x20};
        source_bytes[second + 7] = std::byte{0x01}; // 8193 > the 8192-byte frame buffer
        source_bytes.resize(second + 8193);
    }
    SECTION("second frame extends past the source")
    {
        const std::size_t oversized = source_bytes.size() - second + 1;
        REQUIRE(oversized < 8192);
        source_bytes[second + 6] = static_cast<std::byte>(oversized >> 8U);
        source_bytes[second + 7] = static_cast<std::byte>(oversized);
    }
    SECTION("second frame declares fewer bytes than its header")
    {
        source_bytes[second + 6] = std::byte{0};
        source_bytes[second + 7] = std::byte{7};
    }
    SECTION("decoded second frame exceeds the file's declared sample count")
    {
        data.bytes[4] = std::byte{0};
        data.bytes[5] = std::byte{0};
        data.bytes[6] = std::byte{0x14};
        data.bytes[7] = std::byte{0x01}; // metadata declares 5121, source still contains 6000 frames
    }
    data.byte_length = source_bytes.size();
    const unpackrat::io::byte_source source{
        source_bytes.size(),
        [&](const std::uint64_t offset, const std::span<std::byte> output) {
            REQUIRE(offset <= source_bytes.size());
            REQUIRE(output.size() <= source_bytes.size() - offset);
            std::copy_n(std::span<const std::byte>(source_bytes).subspan(static_cast<std::size_t>(offset)).begin(),
                        output.size(), output.begin());
        },
    };
    std::uint64_t written = 0;
    REQUIRE_THROWS_AS(unpackrat::godot::write_audio_resource_streaming(
                          metadata, source, [&](const std::span<const std::byte> chunk) { written += chunk.size(); }),
                      audio_error);
    REQUIRE(written < 44 + 6000 * 2);
}

TEST_CASE("godot audio: typed MP3 with a truncated first frame fails at Extract", "[godot][audio][mp3-stream-validate]")
{
    const godot_resource_bytes truncated = {0xFF, 0xFB, 0x90, 0x64};
    const auto serialized = build_resource(text_document("AudioStreamMP3", truncated));
    const unpackrat::io::byte_source source{
        serialized.size(),
        [&](const std::uint64_t offset, const std::span<std::byte> output) {
            REQUIRE(offset <= serialized.size());
            REQUIRE(output.size() <= serialized.size() - offset);
            for (std::size_t i = 0; i < output.size(); ++i) {
                output[i] = std::byte{serialized[static_cast<std::size_t>(offset) + i]};
            }
        },
    };
    const binary_resource metadata = unpackrat::godot::read_binary_resource_metadata(source.size, source.read);
    const auto info = unpackrat::godot::inspect_audio_resource_metadata(metadata);
    REQUIRE(info.has_value());
    if (!info.has_value()) {
        return;
    }
    REQUIRE(info.value().extension == "mp3");
    std::size_t written = 0;
    REQUIRE_THROWS_AS(unpackrat::godot::write_audio_resource_streaming(
                          metadata, source, [&](const std::span<const std::byte> chunk) { written += chunk.size(); }),
                      audio_error);
    REQUIRE(written == 0);
}

TEST_CASE("godot audio: Extract validates ID3 extent and a free-format first frame without materializing the tag",
          "[godot][audio][mp3-stream-validate]")
{
    const auto exercise = [](const godot_resource_bytes &payload, const bool valid) {
        const auto serialized = build_resource(text_document("AudioStreamMP3", payload));
        const unpackrat::io::byte_source source{
            serialized.size(),
            [&](const std::uint64_t offset, const std::span<std::byte> output) {
                REQUIRE(offset <= serialized.size());
                REQUIRE(output.size() <= 32768);
                REQUIRE(output.size() <= serialized.size() - offset);
                for (std::size_t i = 0; i < output.size(); ++i) {
                    output[i] = std::byte{serialized[static_cast<std::size_t>(offset) + i]};
                }
            },
        };
        const binary_resource metadata = unpackrat::godot::read_binary_resource_metadata(source.size, source.read);
        const auto info = unpackrat::godot::inspect_audio_resource_metadata(metadata);
        REQUIRE(info.has_value());
        if (!info.has_value()) {
            return;
        }
        REQUIRE(info.value().extension == "mp3");
        std::vector<std::byte> actual;
        const auto write = [&] {
            unpackrat::godot::write_audio_resource_streaming(
                metadata, source, [&](const std::span<const std::byte> chunk) {
                    actual.insert(actual.end(), chunk.begin(), chunk.end());
                });
        };
        if (valid) {
            REQUIRE_NOTHROW(write());
            REQUIRE(actual ==
                    std::vector<std::byte>(reinterpret_cast<const std::byte *>(payload.data()),
                                           reinterpret_cast<const std::byte *>(payload.data()) + payload.size()));
        } else {
            REQUIRE_THROWS_AS(write(), audio_error);
            REQUIRE(actual.empty());
        }
    };

    SECTION("ID3 header without a frame")
    {
        exercise({'I', 'D', '3', 4, 0, 0, 0, 0, 0, 0}, false);
    }
    SECTION("ID3 declares a tag longer than the payload")
    {
        exercise({'I', 'D', '3', 4, 0, 0, 0, 0, 0, 20}, false);
    }
    SECTION("ID3 syncsafe length rejects a high-bit size byte")
    {
        exercise({'I', 'D', '3', 4, 0, 0, 0x80, 0, 0, 0}, false);
    }
    SECTION("a 64 KiB ID3 tag precedes a complete free-format stream")
    {
        godot_resource_bytes payload = {'I', 'D', '3', 4, 0, 0, 0, 4, 0, 0};
        payload.insert(payload.end(), 65536, 0);
        const auto frames = make_free_format_mp3(4, false);
        payload.insert(payload.end(), frames.begin(), frames.end());
        exercise(payload, true);
    }
}

TEST_CASE("godot audio: metadata-only streaming preserves the decoded output", "[godot][audio][audio-streaming]")
{
    std::vector<std::byte> encoded;
    resource_document_spec document;
    SECTION("Godot 4 stereo IMA")
    {
        encoded = {std::byte{0x12}, std::byte{0x34}, std::byte{0x56}, std::byte{0x78}};
        document = wave_document(4, "AudioStreamWAV", to_resource(encoded), 2, 8000, true);
    }
    SECTION("Godot 2 mono IMA with the seed prefix and padding nibble")
    {
        encoded = {std::byte{0}, std::byte{0}, std::byte{0}, std::byte{0}, std::byte{0x12}, std::byte{0x34}};
        document = sample_document(to_resource(encoded), "raw", "ima_adpcm", false, 3, 8000);
    }
    SECTION("Godot 4 QOA")
    {
        const std::array<std::int16_t, 5> pcm{0, 1000, -1000, 5, 7};
        encoded = make_qoa(1, 48000, pcm);
        document = wave_document(4, "AudioStreamWAV", to_resource(encoded), 3, 48000, false);
    }
    SECTION("Godot 4 QOA crosses a frame boundary")
    {
        std::vector<std::int16_t> pcm(5123);
        for (std::size_t i = 0; i < pcm.size(); ++i) {
            pcm[i] = static_cast<std::int16_t>(i % 101);
        }
        encoded = make_qoa(1, 48000, pcm);
        document = wave_document(4, "AudioStreamWAV", to_resource(encoded), 3, 48000, false);
    }
    SECTION("Godot 4 IMA keeps decoder state across output chunks")
    {
        encoded.assign(2049, std::byte{0x24});
        document = wave_document(4, "AudioStreamWAV", to_resource(encoded), 2, 8000, false);
    }
    SECTION("Godot 4 PCM16 uses the source byte order")
    {
        encoded = {std::byte{0x34}, std::byte{0x12}, std::byte{0xFE}, std::byte{0xFF}};
        document = wave_document(4, "AudioStreamWAV", to_resource(encoded), 1, 8000, false);
    }
    SECTION("Godot 3 Ogg is copied in bounded chunks")
    {
        encoded = {std::byte{0x4F}, std::byte{0x67}, std::byte{0x67}, std::byte{0x53}, std::byte{0}};
        document = text_document("AudioStreamOGGVorbis", to_resource(encoded));
    }
    const auto serialized = build_resource(document);
    const auto expected = collect(read(serialized));
    std::size_t largest_read = 0;
    const unpackrat::io::byte_source source{
        serialized.size(),
        [&](const std::uint64_t offset, const std::span<std::byte> output) {
            REQUIRE(offset <= serialized.size());
            REQUIRE(output.size() <= serialized.size() - offset);
            largest_read = (std::max)(largest_read, output.size());
            for (std::size_t i = 0; i < output.size(); ++i) {
                output[i] = std::byte{serialized[static_cast<std::size_t>(offset) + i]};
            }
        },
    };
    const binary_resource resource = unpackrat::godot::read_binary_resource_metadata(source.size, source.read);
    std::vector<std::byte> actual;
    unpackrat::godot::write_audio_resource_streaming(resource, source, [&](const std::span<const std::byte> chunk) {
        actual.insert(actual.end(), chunk.begin(), chunk.end());
    });
    REQUIRE(actual == expected);
    REQUIRE(largest_read <= 8192);
}

TEST_CASE("godot audio: streamed QOA keeps its frame scratch off the source callback stack",
          "[godot][audio][audio-stack]")
{
    const std::array<std::int16_t, 5> pcm{0, 1000, -1000, 5, 7};
    const auto encoded = make_qoa(1, 48000, pcm);
    const auto serialized = build_resource(wave_document(4, "AudioStreamWAV", to_resource(encoded), 3, 48000, false));
    std::byte outer_marker{};
    const auto outer_address = reinterpret_cast<std::uintptr_t>(&outer_marker);
    std::uintptr_t farthest = 0;
    bool measure = false;
    const unpackrat::io::byte_source source{
        serialized.size(),
        [&](const std::uint64_t offset, const std::span<std::byte> destination) {
            std::byte inner_marker{};
            if (measure) {
                const auto inner_address = reinterpret_cast<std::uintptr_t>(&inner_marker);
                farthest = (std::max)(farthest, outer_address > inner_address ? outer_address - inner_address
                                                                              : inner_address - outer_address);
            }
            REQUIRE(offset <= serialized.size());
            REQUIRE(destination.size() <= serialized.size() - offset);
            for (std::size_t index = 0; index < destination.size(); ++index) {
                destination[index] = std::byte{serialized[static_cast<std::size_t>(offset) + index]};
            }
        },
    };
    const auto metadata = unpackrat::godot::read_binary_resource_metadata(source.size, source.read);
    measure = true;
    std::size_t written = 0;
    unpackrat::godot::write_audio_resource_streaming(
        metadata, source, [&](const std::span<const std::byte> bytes) { written += bytes.size(); });
    INFO(farthest);
    CHECK(written == 44 + pcm.size() * 2);
    CHECK(farthest < std::uintptr_t{20} * 1024);
}

TEST_CASE("godot audio: packet-sequence streaming preserves packet payloads and granules",
          "[godot][audio][audio-streaming]")
{
    const auto ident = vorbis_identification(1, 44100);
    const auto comment = vorbis_comment();
    const auto setup = vorbis_setup();
    const std::vector<std::uint8_t> audio{0xAB, 0xCD, 0xEF};
    const auto document = ogg_document({{ident}, {comment, setup}, {audio, audio}}, {0, 1000, 2000}, 44100.0);
    const auto raw = build_resource(document);
    const binary_resource resource = read(raw);
    const unpackrat::io::byte_source source{
        raw.size(),
        [&](const std::uint64_t offset, const std::span<std::byte> output) {
            REQUIRE(offset <= raw.size());
            REQUIRE(output.size() <= raw.size() - offset);
            for (std::size_t i = 0; i < output.size(); ++i) {
                output[i] = std::byte{raw[static_cast<std::size_t>(offset) + i]};
            }
        },
    };
    std::vector<std::byte> actual;
    unpackrat::godot::write_audio_resource_streaming(resource, source, [&](const std::span<const std::byte> chunk) {
        actual.insert(actual.end(), chunk.begin(), chunk.end());
    });
    const auto pages = parse_ogg(actual);
    REQUIRE(pages.first_page_bos);
    REQUIRE(pages.granules.back() == 2000);
    REQUIRE(contains(actual, ident));
    REQUIRE(contains(actual, comment));
    REQUIRE(contains(actual, setup));
    REQUIRE(contains(actual, audio));
}

TEST_CASE("godot audio: finding 4 - QOA stays a Godot 4 AudioStreamWAV format (decision table)",
          "[godot][audio][finding4]")
{
    struct sample_row
    {
        std::uint32_t major;
        const char *class_name;
        std::int64_t format;
        std::uint32_t rate;
        bool supported;
    };
    const std::vector<std::int16_t> pcm = {0, 1000, -1000, 32000};
    const std::vector<sample_row> table = {
        {4, "AudioStreamWAV", 0, 8000, true},      {4, "AudioStreamWAV", 3, 48000, true},
        {3, "AudioStreamSample", 0, 8000, true},   {3, "AudioStreamSample", 2, 8000, true},
        {3, "AudioStreamSample", 3, 48000, false}, {3, "AudioStreamWAV", 3, 48000, false},
        {4, "AudioStreamSample", 0, 8000, false},
    };
    for (const sample_row &row : table) {
        CAPTURE(row.major, row.class_name, row.format, row.rate);
        godot_resource_bytes payload;
        if (row.format == 3) {
            payload = to_resource(make_qoa(1, row.rate, pcm));
        } else if (row.format == 2) {
            payload = {0x11, 0x22};
        } else {
            payload = {0x00, 0x00, 0x00, 0x00};
        }
        const binary_resource resource =
            read(build_resource(wave_document(row.major, row.class_name, payload, row.format, row.rate, false)));
        if (row.supported) {
            CHECK(extension_of(resource) == std::optional<std::string_view>("wav"));
        } else {
            CHECK(!(extension_of(resource).has_value()));
            CHECK_THROWS_AS(collect(resource), audio_error);
        }
    }
}

TEST_CASE("godot audio: finding 5 - MP3 recognition requires a complete Layer III frame", "[godot][audio][finding5]")
{
    const auto mp3 = [](const std::vector<std::uint8_t> &data) {
        return read(build_resource(text_document("AudioStreamMP3", data)));
    };
    const std::vector<std::uint8_t> complete = complete_mp3_frame();

    SECTION("a complete Layer III frame is accepted")
    {
        REQUIRE(extension_of(mp3(complete)) == std::optional<std::string_view>("mp3"));
    }
    SECTION("a frame declaring more bytes than it carries is rejected")
    {
        std::vector<std::uint8_t> truncated = complete;
        truncated.resize(16);
        REQUIRE_THROWS_AS(extension_of(mp3(truncated)), audio_error);
    }
    SECTION("an MPEG-1 Layer II frame is rejected")
    {
        REQUIRE_THROWS_AS(extension_of(mp3(mp3_sized_frame(0xFD, 417))), audio_error);
    }
    SECTION("an MPEG-1 Layer I frame is rejected")
    {
        REQUIRE_THROWS_AS(extension_of(mp3(mp3_sized_frame(0xFF, 417))), audio_error);
    }
}

TEST_CASE("godot audio: finding 6 - the Ogg granule contract is shared by probe and writer", "[godot][audio][finding6]")
{
    const std::vector<std::uint8_t> ident = vorbis_identification(1, 44100);
    const std::vector<std::uint8_t> comment = vorbis_comment();
    const std::vector<std::uint8_t> setup = vorbis_setup();
    const std::vector<std::uint8_t> audio = {0xAB, 0xCD, 0xEF};

    SECTION("a first page carrying a positive granule is rejected by the probe")
    {
        const binary_resource resource = read(build_resource(ogg_document({{ident, comment, setup}}, {1000}, 44100.0)));
        REQUIRE_THROWS_AS(extension_of(resource), audio_error);
        REQUIRE_THROWS_AS(collect(resource), audio_error);
    }
    SECTION("a first page granule of exactly zero is accepted")
    {
        const binary_resource resource = read(build_resource(ogg_document({{ident, comment, setup}}, {0}, 44100.0)));
        REQUIRE(extension_of(resource) == std::optional<std::string_view>("ogg"));
    }
    SECTION("page-final granules survive exactly and in order")
    {
        const binary_resource resource =
            read(build_resource(ogg_document({{ident}, {comment, setup}, {audio, audio}}, {0, 1000, 2000}, 44100.0)));
        REQUIRE(extension_of(resource) == std::optional<std::string_view>("ogg"));
        const parsed_ogg ogg = parse_ogg(collect(resource));
        REQUIRE(ogg.first_page_bos);
        REQUIRE(ogg.granules == std::vector<std::int64_t>({0, 1000, 2000}));
    }
}

TEST_CASE("godot audio: finding 7 - an out-of-range packet-sequence reference is rejected",
          "[godot][audio][finding7][ub]")
{
    const std::vector<std::uint8_t> ident = vorbis_identification(1, 44100);
    const std::vector<std::uint8_t> comment = vorbis_comment();
    const std::vector<std::uint8_t> setup = vorbis_setup();
    const auto document = ogg_document({{ident, comment, setup}}, {0}, 44100.0);
    binary_resource resource = read(build_resource(document));
    REQUIRE(resource.records.size() == 2);
    resource.records.back().properties.back().value.reference.index = 99;
    REQUIRE_THROWS_AS(extension_of(resource), audio_error);
    REQUIRE_THROWS_AS(collect(resource), audio_error);
}

// ---------------------------------------------------------------------------------------------
// Round-2 independent review findings. Each case states the observable requirement and records the
// real outcome against the current production so the required fix has a failing test to drive it.
// ---------------------------------------------------------------------------------------------

TEST_CASE("godot audio: round 2 finding 1 - IMA and QOA wave descriptors preflight PCM geometry",
          "[godot][audio][r2f1]")
{
    // Two headerless mono IMA bytes carry four nibble frames; four mono QOA frames are the same
    // duration. Both expand to a 44 + 4 * 2 = 52 byte canonical WAVE.
    const std::vector<std::uint8_t> ima2 = {0x11, 0x22};
    const std::vector<std::int16_t> qoa_pcm = {0, 1000, -1000, 32000};
    const std::vector<std::uint8_t> qoa4 = to_unsigned(make_qoa(1, 8000, qoa_pcm));

    struct wave_row
    {
        std::uint32_t major;
        const char *class_name;
        std::int64_t format;
        const std::vector<std::uint8_t> *payload;
        std::size_t frames;
    };
    const std::vector<wave_row> table = {
        {3, "AudioStreamSample", 2, &ima2, 4},
        {4, "AudioStreamWAV", 2, &ima2, 4},
        {4, "AudioStreamWAV", 3, &qoa4, 4},
    };

    for (const wave_row &row : table) {
        CAPTURE(row.major, row.class_name, row.format);
        const auto document = [&row] {
            return read(build_resource(
                wave_document(row.major, row.class_name, as_resource(*row.payload), row.format, 8000, false)));
        };
        const std::size_t wave_bytes = 44 + row.frames * 2;

        // The exact canonical WAVE size and the exact frame count are advertised, and the writer
        // produces exactly that stream within the same budget.
        audio_limits exact;
        exact.max_pcm_frames = row.frames;
        exact.max_output_bytes = wave_bytes;
        CHECK(extension_of(document(), exact) == std::optional<std::string_view>("wav"));
        CHECK(collect(document(), exact).size() == wave_bytes);

        // One byte short of the whole WAVE is rejected at the extension probe, before any decode.
        audio_limits out_short;
        out_short.max_output_bytes = wave_bytes - 1;
        CHECK_THROWS_AS(extension_of(document(), out_short), audio_error);
        CHECK_THROWS_AS(collect(document(), out_short), audio_error);

        // One frame short of the budget is rejected at the extension probe too.
        audio_limits frame_short;
        frame_short.max_pcm_frames = row.frames - 1;
        CHECK_THROWS_AS(extension_of(document(), frame_short), audio_error);
        CHECK_THROWS_AS(collect(document(), frame_short), audio_error);
    }
}

TEST_CASE("godot audio: round 2 finding 1 - structurally invalid IMA descriptors are rejected first",
          "[godot][audio][r2f1]")
{
    SECTION("an empty IMA payload has no frame")
    {
        const binary_resource resource =
            read(build_resource(wave_document(4, "AudioStreamWAV", godot_resource_bytes{}, 2, 8000, false)));
        REQUIRE_THROWS_AS(extension_of(resource), audio_error);
        REQUIRE_THROWS_AS(collect(resource), audio_error);
    }
    SECTION("a stereo IMA payload with an odd byte count cannot interleave")
    {
        const godot_resource_bytes odd = {0x11, 0x22, 0x33};
        const binary_resource resource = read(build_resource(wave_document(4, "AudioStreamWAV", odd, 2, 8000, true)));
        REQUIRE_THROWS_AS(extension_of(resource), audio_error);
        REQUIRE_THROWS_AS(collect(resource), audio_error);
    }
    SECTION("a stereo IMA payload of whole interleaved pairs is advertised")
    {
        const godot_resource_bytes even = {0x11, 0x22, 0x33, 0x44};
        const binary_resource resource = read(build_resource(wave_document(4, "AudioStreamWAV", even, 2, 8000, true)));
        REQUIRE(extension_of(resource) == std::optional<std::string_view>("wav"));
    }
}

TEST_CASE("godot audio: round 2 finding 2 - a Godot 2 IMA declared padding nibble stays within the budget",
          "[godot][audio][r2f2]")
{
    // Three headerless mono IMA bytes carry six nibble frames; the source declares five, so the
    // final padding nibble must not be decoded and the canonical WAVE is 44 + 5 * 2 = 54 bytes.
    const std::vector<std::uint8_t> packed = {0x01, 0x23, 0x45};
    const godot_resource_bytes raw = to_resource(make_ima_mono(packed));
    const binary_resource resource = read(build_resource(sample_document(raw, "raw", "ima_adpcm", false, 5, 44100)));
    const std::vector<std::int16_t> reference = ima_reference_pcm(packed, 1);
    const std::vector<std::int16_t> expected(reference.begin(), reference.begin() + 5);

    SECTION("the probe advertises the declared five-frame duration")
    {
        audio_limits limits;
        limits.max_pcm_frames = 5;
        limits.max_output_bytes = 44 + 5 * 2;
        REQUIRE(extension_of(resource, limits) == std::optional<std::string_view>("wav"));
    }
    SECTION("the writer produces exactly the five declared frames inside the budget")
    {
        audio_limits limits;
        limits.max_pcm_frames = 5;
        limits.max_output_bytes = 44 + 5 * 2;
        const std::vector<std::byte> bytes = collect(resource, limits);
        REQUIRE(bytes.size() == 54);
        REQUIRE(parse_wave(bytes).samples == expected);
    }
    SECTION("a frame budget below the declared count is rejected before advertising")
    {
        audio_limits limits;
        limits.max_pcm_frames = 4;
        REQUIRE_THROWS_AS(extension_of(resource, limits), audio_error);
    }
}

TEST_CASE("godot audio: round 3 finding 1 - a Godot 2 stereo IMA Sample is structurally preflighted",
          "[godot][audio][r3f1]")
{
    // A stereo IMA payload must be a whole number of byte-interleaved pairs. Three bytes with a
    // declared length of three frames would otherwise be advertised as WAVE even though the codec
    // rejects the odd interleave later, so the probe must reject it before exposing the extension.
    const godot_resource_bytes odd = {0x11, 0x22, 0x33};
    const binary_resource odd_resource = read(build_resource(sample_document(odd, "raw", "ima_adpcm", true, 3, 44100)));
    REQUIRE_THROWS_AS(extension_of(odd_resource), audio_error);
    REQUIRE_THROWS_AS(collect(odd_resource), audio_error);

    // A payload shorter than one interleaved frame has no complete frame at all.
    const binary_resource empty_resource =
        read(build_resource(sample_document({}, "raw", "ima_adpcm", true, 1, 44100)));
    REQUIRE_THROWS_AS(extension_of(empty_resource), audio_error);

    // A whole number of interleaved pairs is still advertised and decodes to the reference PCM.
    const std::vector<std::uint8_t> packed = {0x11, 0x22, 0x33, 0x44};
    const binary_resource even_resource =
        read(build_resource(sample_document(to_resource(make_ima_mono(packed)), "raw", "ima_adpcm", true, 4, 44100)));
    REQUIRE(extension_of(even_resource) == std::optional<std::string_view>("wav"));
    REQUIRE(parse_wave(collect(even_resource)).samples == ima_reference_pcm(packed, 2));
}

TEST_CASE("godot audio: round 3 finding 2 - a free-format MP3 passes through the converter", "[godot][audio][r3f2]")
{
    // An AudioStreamMP3 whose payload is a free-format Layer III stream must still be recognised and
    // forwarded verbatim; the probe reproduces the decoder's learned free-format frame size.
    const std::vector<std::uint8_t> free_format = make_free_format_mp3(4, false);
    const binary_resource resource = read(build_resource(text_document("AudioStreamMP3", free_format)));
    REQUIRE(extension_of(resource) == std::optional<std::string_view>("mp3"));
    const std::vector<std::byte> output = collect(resource);
    REQUIRE(output.size() == free_format.size());
    REQUIRE(std::memcmp(output.data(), free_format.data(), free_format.size()) == 0);
}

TEST_CASE("godot audio: finding 7 - the packet-sequence packet ceiling is enforced before the vector grows",
          "[godot][audio][finding7]")
{
    using unpackrat::godot::resource_property;
    using unpackrat::godot::resource_record;
    using unpackrat::godot::resource_value;
    using unpackrat::godot::value_kind;

    // The muxer's default packet ceiling (codecs::ogg_limits::max_packets). The converter must
    // refuse the page before the borrowed packet vector grows past it, so a hostile descriptor
    // cannot drive an unbounded allocation; the descriptor is built directly because the reader's
    // own node budget would reject this many nodes first.
    constexpr std::size_t packet_ceiling = 65536;

    resource_value page;
    page.kind = value_kind::array;
    page.array.resize(packet_ceiling + 1);
    for (resource_value &packet : page.array) {
        packet.kind = value_kind::bytes;
        packet.bytes = {std::byte{0}};
    }

    resource_value pages;
    pages.kind = value_kind::array;
    pages.array.push_back(std::move(page));

    resource_value granules;
    granules.kind = value_kind::integer_array;
    granules.integer_array = {-1};

    resource_value rate;
    rate.kind = value_kind::real;
    rate.real = 44100.0;

    binary_resource resource;
    resource.engine = {4, 5};
    resource.format_version = 6;
    resource.main_type = "AudioStreamOggVorbis";
    resource.records.resize(2);

    resource_record &sequence = resource.records[0];
    sequence.path = "local://1";
    sequence.type = "OggPacketSequence";
    sequence.properties = {
        resource_property{"packet_data", std::move(pages)},
        resource_property{"granule_positions", std::move(granules)},
        resource_property{"sampling_rate", std::move(rate)},
    };

    resource_value reference;
    reference.kind = value_kind::reference;
    reference.reference.index = 0;

    resource_record &root = resource.records[1];
    root.type = "AudioStreamOggVorbis";
    root.properties = {resource_property{"packet_sequence", std::move(reference)}};

    REQUIRE_THROWS_AS(extension_of(resource), audio_error);
    REQUIRE_THROWS_AS(collect(resource), audio_error);
}
TEST_CASE("godot audio: class admission recognizes only supported root audio classes",
          "[godot][audio][class-admission]")
{
    const std::pair<std::string_view, bool> cases[] = {
        {"Sample", true},
        {"AudioStreamSample", true},
        {"AudioStreamWAV", true},
        {"AudioStreamMP3", true},
        {"AudioStreamOGGVorbis", true},
        {"AudioStreamOggVorbis", true},
        {"OggPacketSequence", false},
        {"audiostreammp3", false},
        {"AudioStreamOggVorbis2", false},
        {"ImageTexture", false},
        {"", false},
    };
    for (const auto &[class_name, expected] : cases) {
        CAPTURE(class_name);
        CHECK(unpackrat::godot::supports_audio_resource_class(class_name) == expected);
    }
}
