#include "audio_resource.h"

#include "../codecs/audio_preflight.h"
#include "../codecs/godot_ima_codec.h"
#include "../codecs/mp3_codec.h"
#include "../codecs/ogg_codec.h"
#include "../codecs/qoa_codec.h"
#include "../codecs/wav_codec.h"

#include <algorithm>
#include <array>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

namespace unpackrat::godot
{
    namespace
    {
        using codecs::audio_error;

        // The sample rate Godot can carry in every supported container (the QOA frame header stores
        // it in 24 bits); anything above it is treated as a malformed descriptor, not a rate.
        constexpr std::uint32_t max_supported_sample_rate = 0xFFFFFFU;
        constexpr unsigned max_supported_channels = 2;

        enum class audio_container : std::uint8_t
        {
            wav,
            mp3,
            ogg,
        };

        // The Godot sample families that share a raw-payload/frame layout.
        enum class wav_encoding : std::uint8_t
        {
            pcm8,
            pcm16,
            ima,
            qoa,
        };

        // The shared result of descriptor resolution. Only the fields named by ``container`` (and,
        // for WAVE, ``encoding``) are meaningful. ``embedded`` and every packet borrow the byte
        // vectors owned by the ``binary_resource`` argument, so they stay valid for exactly as long
        // as the caller keeps that resource alive.
        struct resolved_audio final
        {
            audio_container container = audio_container::wav;
            std::span<const std::byte> embedded; // mp3 / Godot 3 Ogg: exact bytes to forward
            wav_encoding encoding = wav_encoding::pcm8;
            std::span<const std::byte> payload; // wav: the raw sample payload
            std::uint32_t channels = 0;
            std::uint32_t sample_rate = 0;
            // wav/ima: the declared Godot 2 frame count when the payload carries a length field and
            // the trailing nibble padding must be trimmed; zero means the whole decoded payload is
            // meaningful (every other sample layout).
            std::uint64_t sample_frames = 0;
            std::uint64_t output_size = 0;
            std::uint8_t mp3_leading_bytes = 0;
            std::vector<codecs::ogg_packet_view> packets; // Godot 4 Ogg: packets to remux
        };

        unsigned byte_value(const std::byte value) noexcept
        {
            return std::to_integer<unsigned>(value);
        }

        std::uint16_t read_u16_be(const std::byte *const bytes) noexcept
        {
            return static_cast<std::uint16_t>((byte_value(bytes[0]) << 8U) | byte_value(bytes[1]));
        }

        std::uint32_t read_u24_be(const std::byte *const bytes) noexcept
        {
            return (byte_value(bytes[0]) << 16U) | (byte_value(bytes[1]) << 8U) | byte_value(bytes[2]);
        }

        std::uint32_t read_u32_be(const std::byte *const bytes) noexcept
        {
            return (static_cast<std::uint32_t>(byte_value(bytes[0])) << 24U) |
                   (static_cast<std::uint32_t>(byte_value(bytes[1])) << 16U) |
                   (static_cast<std::uint32_t>(byte_value(bytes[2])) << 8U) |
                   static_cast<std::uint32_t>(byte_value(bytes[3]));
        }

        std::uint32_t read_u32_le(const std::byte *const bytes) noexcept
        {
            return static_cast<std::uint32_t>(byte_value(bytes[0])) |
                   (static_cast<std::uint32_t>(byte_value(bytes[1])) << 8U) |
                   (static_cast<std::uint32_t>(byte_value(bytes[2])) << 16U) |
                   (static_cast<std::uint32_t>(byte_value(bytes[3])) << 24U);
        }

        std::span<const std::byte> as_span(const std::vector<std::byte> &bytes) noexcept
        {
            return {bytes.data(), bytes.size()};
        }

        bool matches(const std::span<const std::byte> bytes, const char *const text, const std::size_t length) noexcept
        {
            if (bytes.size() < length) {
                return false;
            }
            for (std::size_t index = 0; index < length; ++index) {
                if (byte_value(bytes[index]) != static_cast<unsigned char>(text[index])) {
                    return false;
                }
            }
            return true;
        }

        [[noreturn]] void malformed(const std::string_view detail)
        {
            std::string message = "godot audio: ";
            message.append(detail);
            throw audio_error(message);
        }

        // Finds the single property named ``name``. A second occurrence is a duplicate that would
        // make the descriptor ambiguous and is reported rather than silently collapsed.
        const resource_value *find_property(const resource_record &record, const std::string_view name)
        {
            const resource_value *found = nullptr;
            for (const resource_property &property : record.properties) {
                if (std::string_view(property.name) == name) {
                    if (found != nullptr) {
                        malformed("a record carries a duplicate field");
                    }
                    found = &property.value;
                }
            }
            return found;
        }

        const resource_value &require_property(const resource_record &record, const std::string_view name)
        {
            const resource_value *const value = find_property(record, name);
            if (value == nullptr) {
                malformed("a required descriptor field is missing");
            }
            return *value;
        }

        const resource_value &require_bytes(const resource_record &record, const std::string_view name)
        {
            const resource_value &value = require_property(record, name);
            if (value.kind != value_kind::bytes) {
                malformed("a byte payload field has the wrong type");
            }
            return value;
        }

        std::uint64_t stored_byte_length(const resource_value &value, const bool metadata_only)
        {
            if (!metadata_only) {
                return value.bytes.size();
            }
            const std::uint64_t length = value.byte_length == 0 ? value.bytes.size() : value.byte_length;
            if (length < value.bytes.size()) {
                malformed("a captured byte prefix exceeds its declared payload length");
            }
            return length;
        }

        bool boolean_or_default(const resource_record &record, const std::string_view name, const bool fallback)
        {
            const resource_value *const value = find_property(record, name);
            if (value == nullptr) {
                return fallback;
            }
            if (value->kind != value_kind::boolean) {
                malformed("a boolean field has the wrong type");
            }
            return value->boolean;
        }

        std::int64_t require_integer(const resource_record &record, const std::string_view name)
        {
            const resource_value &value = require_property(record, name);
            if (value.kind != value_kind::integer) {
                malformed("an integer field has the wrong type");
            }
            return value.integer;
        }

        std::int64_t integer_or_default(const resource_record &record, const std::string_view name,
                                        const std::int64_t fallback)
        {
            const resource_value *const value = find_property(record, name);
            if (value == nullptr) {
                return fallback;
            }
            if (value->kind != value_kind::integer) {
                malformed("an integer field has the wrong type");
            }
            return value->integer;
        }

        double require_real(const resource_record &record, const std::string_view name)
        {
            const resource_value &value = require_property(record, name);
            if (value.kind != value_kind::real) {
                malformed("a real field has the wrong type");
            }
            return value.real;
        }

        const std::vector<resource_property> &require_dictionary(const resource_record &record,
                                                                 const std::string_view name)
        {
            const resource_value &value = require_property(record, name);
            if (value.kind != value_kind::dictionary) {
                malformed("a dictionary field has the wrong type");
            }
            return value.dictionary;
        }

        const resource_value &dict_entry(const std::vector<resource_property> &entries, const std::string_view name)
        {
            const auto found = std::find_if(entries.begin(), entries.end(), [name](const resource_property &entry) {
                return std::string_view(entry.name) == name;
            });
            if (found == entries.end()) {
                malformed("a data dictionary is missing a required key");
            }
            // The reader already rejects a dictionary with a duplicate key, so the first
            // match is the only match.
            return found->value;
        }

        const std::string &dict_string(const std::vector<resource_property> &entries, const std::string_view name)
        {
            const resource_value &value = dict_entry(entries, name);
            if (value.kind != value_kind::string) {
                malformed("a data dictionary string key has the wrong type");
            }
            return value.string;
        }

        const resource_value &dict_bytes(const std::vector<resource_property> &entries, const std::string_view name)
        {
            const resource_value &value = dict_entry(entries, name);
            if (value.kind != value_kind::bytes) {
                malformed("a data dictionary byte key has the wrong type");
            }
            return value;
        }

        bool dict_boolean(const std::vector<resource_property> &entries, const std::string_view name)
        {
            const resource_value &value = dict_entry(entries, name);
            if (value.kind != value_kind::boolean) {
                malformed("a data dictionary boolean key has the wrong type");
            }
            return value.boolean;
        }

        std::int64_t dict_integer(const std::vector<resource_property> &entries, const std::string_view name)
        {
            const resource_value &value = dict_entry(entries, name);
            if (value.kind != value_kind::integer) {
                malformed("a data dictionary integer key has the wrong type");
            }
            return value.integer;
        }

        void check_input_budget(const std::uint64_t size, const codecs::audio_limits &limits)
        {
            if (size > limits.max_input_bytes) {
                malformed("a payload exceeds the configured input byte budget");
            }
        }

        // A byte payload that is forwarded verbatim (MP3, embedded Godot 3 Ogg) has an exactly
        // known output size, so the output budget is decided here, in the shared descriptor
        // resolution, before the watcher reaches the sink. The write path reuses the same
        // resolution, so the advertised extension and the produced bytes can never disagree about
        // the budget.
        void check_output_budget(const std::uint64_t size, const codecs::audio_limits &limits)
        {
            if (size > limits.max_output_bytes) {
                malformed("the output exceeds the configured byte budget");
            }
        }

        // Every PCM sample layout expands to a PCM16 RIFF/WAVE stream. The frame count and the
        // whole canonical container size (the 44-byte header plus the interleaved payload) are
        // decided through the shared, allocation-free preflight before any decode or allocation.
        void check_pcm_geometry(const std::uint64_t frames, const std::uint32_t channels,
                                const codecs::audio_limits &limits)
        {
            if (frames > limits.max_pcm_frames) {
                malformed("a PCM frame count exceeds the configured budget");
            }
            static_cast<void>(codecs::preflight::interleaved_sample_count(frames, channels));
            const std::uint64_t stream = codecs::preflight::riff_pcm16_stream_bytes(frames, channels);
            if (stream > limits.max_output_bytes) {
                malformed("the WAVE output exceeds the configured byte budget");
            }
        }

        std::uint32_t check_sample_rate(const std::int64_t rate)
        {
            if (rate <= 0) {
                malformed("the sample rate is not positive");
            }
            if (static_cast<std::uint64_t>(rate) > max_supported_sample_rate) {
                malformed("the sample rate is out of range");
            }
            return static_cast<std::uint32_t>(rate);
        }

        std::uint32_t channels_for(const bool stereo) noexcept
        {
            return stereo ? 2U : 1U;
        }

        resolved_audio make_wave(const wav_encoding encoding, const std::span<const std::byte> payload,
                                 const std::uint32_t channels, const std::uint32_t sample_rate,
                                 const std::uint64_t frames)
        {
            resolved_audio result;
            result.container = audio_container::wav;
            result.encoding = encoding;
            result.payload = payload;
            result.channels = channels;
            result.sample_rate = sample_rate;
            result.output_size = codecs::preflight::riff_pcm16_stream_bytes(frames, channels);
            return result;
        }

        // A QOA sample stores a complete QOA stream (Godot 4 FORMAT_QOA). The file header names the
        // total sample count and the first frame header repeats the channel count and sample rate;
        // both must agree with the Godot descriptor before the codec is trusted with the payload.
        // The validated total sample count is returned so the caller can preflight the decoded
        // geometry from the header metadata alone, without decoding a single sample.
        std::uint32_t check_qoa_header(const std::span<const std::byte> payload, const std::uint64_t stored_length,
                                       const std::uint32_t channels, const std::uint32_t sample_rate)
        {
            constexpr std::size_t qoa_file_header = 8;
            constexpr std::size_t qoa_frame_header = 8;
            constexpr std::size_t qoa_lms_bytes_per_channel = 16;
            if (!matches(payload, "qoaf", 4)) {
                malformed("a QOA payload has no file capture pattern");
            }
            if (payload.size() < qoa_file_header + qoa_frame_header) {
                malformed("a QOA payload is shorter than its header and first frame");
            }
            if (read_u32_be(payload.data() + 4) == 0) {
                malformed("a QOA header declares zero samples");
            }
            const std::byte *const frame = payload.data() + qoa_file_header;
            if (byte_value(frame[0]) != channels) {
                malformed("a QOA frame disagrees with the descriptor channel count");
            }
            if (read_u24_be(frame + 1) != sample_rate) {
                malformed("a QOA frame disagrees with the descriptor sample rate");
            }
            const std::uint32_t frame_samples = read_u16_be(frame + 4);
            const std::uint32_t total_samples = read_u32_be(payload.data() + 4);
            if (frame_samples == 0) {
                malformed("a QOA frame declares zero samples");
            }
            if (frame_samples > total_samples) {
                malformed("a QOA frame declares more samples than its file header");
            }
            const std::uint32_t frame_size = read_u16_be(frame + 6);
            const std::size_t minimum_frame = qoa_frame_header + qoa_lms_bytes_per_channel * channels;
            if (frame_size < minimum_frame) {
                malformed("a QOA frame is smaller than its state header");
            }
            if (frame_size > stored_length - qoa_file_header) {
                malformed("a QOA frame size exceeds the payload");
            }
            return total_samples;
        }

        // The byte width of one interleaved frame for the raw sample encodings; both expand to
        // PCM16, so a packed 8-bit frame is one byte per channel and a 16-bit frame two.
        std::size_t pcm_frame_bytes(const wav_encoding encoding, const std::uint32_t channels) noexcept
        {
            const std::size_t per_channel = encoding == wav_encoding::pcm16 ? 2U : 1U;
            return static_cast<std::size_t>(channels) * per_channel;
        }

        // The single entry point for a raw PCM sample payload shared by the Godot 3/4 wave classes
        // and the Godot 2 ``Sample`` dictionary. The payload must be a whole number of frames and
        // has to fit both the frame budget and the whole canonical WAVE size before anything is
        // allocated or decoded.
        std::optional<resolved_audio> resolve_pcm(const resource_value &data, const wav_encoding encoding,
                                                  const std::uint32_t channels, const std::uint32_t sample_rate,
                                                  const codecs::audio_limits &limits, const bool metadata_only)
        {
            const std::size_t frame_bytes = pcm_frame_bytes(encoding, channels);
            const std::uint64_t length = stored_byte_length(data, metadata_only);
            if (length % frame_bytes != 0) {
                malformed("a PCM payload is not a whole number of frames");
            }
            check_pcm_geometry(length / frame_bytes, channels, limits);
            return make_wave(encoding, as_span(data.bytes), channels, sample_rate, length / frame_bytes);
        }

        // The generation a wave-family class belongs to. ``AudioStreamSample`` is the Godot 3 class
        // and ``AudioStreamWAV`` replaced it in Godot 4; a class presented by a different generation
        // is not a stream this converter supports.
        enum class wave_generation : std::uint8_t
        {
            none,
            godot3,
            godot4,
        };

        wave_generation wave_generation_of(const std::uint32_t major, const std::string_view class_name) noexcept
        {
            if (class_name == "AudioStreamSample" && major == 3) {
                return wave_generation::godot3;
            }
            if (class_name == "AudioStreamWAV" && major >= 4) {
                return wave_generation::godot4;
            }
            return wave_generation::none;
        }

        // The generation/format decision table for the wave family. QOA (format 3) exists only in
        // the Godot 4 class; a format outside the table is not a supported stream and stays raw.
        std::optional<wav_encoding> wave_encoding_for(const wave_generation generation,
                                                      const std::int64_t format) noexcept
        {
            switch (format) {
            case 0:
                return wav_encoding::pcm8;
            case 1:
                return wav_encoding::pcm16;
            case 2:
                return wav_encoding::ima;
            case 3:
                return generation == wave_generation::godot4 ? std::optional<wav_encoding>(wav_encoding::qoa)
                                                             : std::nullopt;
            default:
                return std::nullopt;
            }
        }

        // A Godot 3/4 IMA payload is a headerless packed run: every byte carries two nibbles across
        // the interleaved lanes, so a mono payload carries twice its byte count in frames and a
        // stereo payload one frame per interleaved pair. Whole interleaved pairs and at least one
        // frame are required, and the derived frame count and the whole canonical WAVE size are
        // decided through the shared preflight before the extension is advertised, exactly as the
        // writer's decode will later measure them.
        std::optional<resolved_audio> resolve_wave_ima(const resource_value &data, const std::uint32_t channels,
                                                       const std::uint32_t sample_rate,
                                                       const codecs::audio_limits &limits, const bool metadata_only)
        {
            const std::uint64_t length = stored_byte_length(data, metadata_only);
            if (length < channels) {
                malformed("an IMA payload is shorter than one interleaved frame");
            }
            if (channels == 2 && length % 2 != 0) {
                malformed("a stereo IMA payload must be byte-interleaved");
            }
            const std::uint64_t frames = codecs::preflight::ima_frame_capacity(length, channels);
            check_pcm_geometry(frames, channels, limits);
            return make_wave(wav_encoding::ima, as_span(data.bytes), channels, sample_rate, frames);
        }

        // Resolves the Godot 3/4 sample family (AudioStreamSample / AudioStreamWAV), whose ``data``
        // is the raw sample payload and whose ``format`` is the integer enum. The engine generation
        // from the container header gates which class/format combinations are accepted.
        std::optional<resolved_audio> resolve_wave_record(const resource_record &record, const std::uint32_t major,
                                                          const codecs::audio_limits &limits, const bool metadata_only)
        {
            const wave_generation generation = wave_generation_of(major, record.type);
            if (generation == wave_generation::none) {
                return std::nullopt;
            }
            const resource_value &data = require_bytes(record, "data");
            // Godot's binary saver omits properties equal to their class defaults. The pinned
            // Godot 3 AudioStreamSample constructor and Godot 4 AudioStreamWAV header both use
            // PCM8, 44100 Hz and mono; present properties still require their exact wire types.
            const std::int64_t format = integer_or_default(record, "format", 0);
            const std::uint32_t sample_rate = check_sample_rate(integer_or_default(record, "mix_rate", 44100));
            const std::uint32_t channels = channels_for(boolean_or_default(record, "stereo", false));
            const std::uint64_t length = stored_byte_length(data, metadata_only);
            check_input_budget(length, limits);

            const std::optional<wav_encoding> encoding = wave_encoding_for(generation, format);
            if (!encoding.has_value()) {
                return std::nullopt;
            }
            if (*encoding == wav_encoding::pcm8 || *encoding == wav_encoding::pcm16) {
                return resolve_pcm(data, *encoding, channels, sample_rate, limits, metadata_only);
            }
            if (*encoding == wav_encoding::ima) {
                return resolve_wave_ima(data, channels, sample_rate, limits, metadata_only);
            }
            const std::uint32_t qoa_samples = check_qoa_header(as_span(data.bytes), length, channels, sample_rate);
            // The QOA file header names the total sample count, so the decoded geometry can be
            // preflighted from that metadata before the extension is advertised; the samples
            // themselves are still only decoded by the writer.
            check_pcm_geometry(qoa_samples, channels, limits);
            return make_wave(wav_encoding::qoa, as_span(data.bytes), channels, sample_rate, qoa_samples);
        }

        // A Sample may carry a headerless nibble stream or four zero seed bytes per channel before
        // its nibble stream. The latter layout occurs in the legacy corpus; those seed bytes do not
        // represent source frames. The declared length distinguishes the layouts and trims a final
        // padding nibble. Only the nibble stream is passed to the shared decoder.
        std::optional<resolved_audio> resolve_sample_ima(const resource_value &data, const std::uint32_t channels,
                                                         const std::uint32_t sample_rate, const std::uint64_t frames,
                                                         const codecs::audio_limits &limits, const bool metadata_only)
        {
            // The same structural preflight the decoder applies before it derives the capacity: a
            // payload needs at least one whole interleaved frame (one byte per channel) and, for
            // stereo, a whole number of byte-interleaved pairs. Deciding this here keeps the
            // advertised extension and the writer's later decode in agreement instead of advertising
            // WAVE for a payload the codec will reject.
            const std::uint64_t length = stored_byte_length(data, metadata_only);
            if (length < channels) {
                malformed("a Sample IMA payload is shorter than one interleaved frame");
            }
            if (channels == 2 && length % 2 != 0) {
                malformed("a stereo Sample IMA payload must be byte-interleaved");
            }
            const std::uint64_t capacity = codecs::preflight::ima_frame_capacity(length, channels);
            if (frames > capacity) {
                malformed("a Sample IMA length exceeds the encoded capacity");
            }
            if (frames == 0) {
                malformed("a Sample IMA length disagrees with the encoded capacity");
            }
            const std::uint64_t surplus = capacity - frames;
            std::size_t prefix_bytes = 0;
            if (surplus == 8 || surplus == 9) {
                prefix_bytes = std::size_t{4} * channels;
                if (data.bytes.size() < prefix_bytes) {
                    malformed("a Sample IMA seed prefix is truncated");
                }
                const auto prefix = std::span<const std::byte>(data.bytes).first(prefix_bytes);
                if (!std::all_of(prefix.begin(), prefix.end(),
                                 [](const std::byte value) { return value == std::byte{0}; })) {
                    malformed("a Sample IMA seed prefix is not zero");
                }
            } else if (surplus > 1) {
                malformed("a Sample IMA length disagrees with the encoded capacity");
            }
            check_pcm_geometry(frames, channels, limits);
            resolved_audio result =
                make_wave(wav_encoding::ima, as_span(data.bytes).subspan(prefix_bytes), channels, sample_rate, frames);
            result.sample_frames = frames;
            return result;
        }

        // Resolves the Godot 2 ``Sample`` resource, whose ``data`` is a dictionary with string
        // packing/format names and whose rate lives in a record-level ``mix_rate``.
        std::optional<resolved_audio> resolve_sample_record(const resource_record &record,
                                                            const codecs::audio_limits &limits,
                                                            const bool metadata_only)
        {
            const std::uint32_t sample_rate = check_sample_rate(require_integer(record, "mix_rate"));
            const std::vector<resource_property> &entries = require_dictionary(record, "data");
            const std::string &packing = dict_string(entries, "packing");
            const std::string &format = dict_string(entries, "format");
            const std::uint32_t channels = channels_for(dict_boolean(entries, "stereo"));
            const std::int64_t length = dict_integer(entries, "length");
            const resource_value &data = dict_bytes(entries, "data");
            const std::uint64_t payload_length = stored_byte_length(data, metadata_only);
            check_input_budget(payload_length, limits);
            if (length < 0) {
                malformed("a Sample length is negative");
            }
            if (packing != "raw") {
                return std::nullopt;
            }
            const std::uint64_t frames = static_cast<std::uint64_t>(length);
            if (format == "pcm8" || format == "pcm16") {
                const wav_encoding encoding = format == "pcm16" ? wav_encoding::pcm16 : wav_encoding::pcm8;
                // The derived count comes from the payload length (resolve_pcm rejects a
                // misaligned one first); the declared length must match it exactly.
                const std::optional<resolved_audio> wave =
                    resolve_pcm(data, encoding, channels, sample_rate, limits, metadata_only);
                if (frames != payload_length / pcm_frame_bytes(encoding, channels)) {
                    malformed("a Sample length disagrees with its payload");
                }
                return wave;
            }
            if (format == "ima_adpcm") {
                return resolve_sample_ima(data, channels, sample_rate, frames, limits, metadata_only);
            }
            return std::nullopt;
        }

        // Resolves the Godot 3 Ogg stream, whose ``data`` is the complete Ogg bitstream. A resource
        // without a byte payload (the Godot 2 filename-only form) resolves to no extension.
        std::optional<resolved_audio> resolve_ogg_bytes(const resource_record &record,
                                                        const codecs::audio_limits &limits, const bool metadata_only)
        {
            const resource_value *const value = find_property(record, "data");
            if (value == nullptr) {
                return std::nullopt;
            }
            if (value->kind != value_kind::bytes) {
                malformed("an Ogg payload field has the wrong type");
            }
            const std::uint64_t length = stored_byte_length(*value, metadata_only);
            check_input_budget(length, limits);
            check_output_budget(length, limits);
            const std::span<const std::byte> data = as_span(value->bytes);
            if (!matches(data, "OggS", 4)) {
                malformed("an Ogg payload has no capture pattern");
            }
            if (data.size() < 5) {
                malformed("an Ogg payload is shorter than its capture header");
            }
            if (byte_value(data[4]) != 0) {
                malformed("an Ogg payload declares an unsupported bitstream version");
            }
            resolved_audio result;
            result.container = audio_container::ogg;
            result.embedded = data;
            result.output_size = length;
            return result;
        }

        void check_vorbis_header(const std::span<const std::byte> packet, const unsigned type)
        {
            if (packet.size() < 7) {
                malformed("a Vorbis header packet is truncated");
            }
            if (byte_value(packet[0]) != type) {
                malformed("a Vorbis header packet has the wrong type byte");
            }
            if (!matches(packet.subspan(1), "vorbis", 6)) {
                malformed("a Vorbis header packet has no vorbis signature");
            }
        }

        void check_vorbis_identification(const std::span<const std::byte> packet, const double declared_rate)
        {
            if (packet.size() < 30) {
                malformed("a Vorbis identification header is truncated");
            }
            const std::byte *const bytes = packet.data();
            if (read_u32_le(bytes + 7) != 0) {
                malformed("a Vorbis identification header declares an unsupported version");
            }
            const unsigned channels = byte_value(bytes[11]);
            if (channels == 0) {
                malformed("a Vorbis identification header declares no channels");
            }
            if (channels > max_supported_channels) {
                malformed("a Vorbis identification header declares too many channels");
            }
            const std::uint32_t rate = read_u32_le(bytes + 12);
            if (rate == 0) {
                malformed("a Vorbis identification header declares no sample rate");
            }
            if (static_cast<double>(rate) != declared_rate) {
                malformed("a Vorbis identification header disagrees with the declared sample rate");
            }
            if ((byte_value(bytes[29]) & 0x01U) == 0) {
                malformed("a Vorbis identification header has no framing flag");
            }
        }

        // Resolves the Godot 4 form: the root ``AudioStreamOggVorbis`` references an internal
        // ``OggPacketSequence`` whose pages of packets are remuxed into an Ogg stream.
        std::optional<resolved_audio> resolve_packet_sequence(const binary_resource &resource,
                                                              const resource_reference &reference,
                                                              const codecs::audio_limits &limits,
                                                              const bool metadata_only)
        {
            if (reference.empty) {
                malformed("an Ogg packet-sequence reference is null");
            }
            if (reference.external) {
                malformed("an Ogg packet-sequence reference is external");
            }
            if (reference.index >= resource.records.size()) {
                malformed("an Ogg packet-sequence reference is out of range");
            }
            const resource_record &sequence = resource.records[reference.index];
            if (sequence.type != "OggPacketSequence") {
                malformed("an Ogg packet-sequence reference points at the wrong class");
            }
            const double sampling_rate = require_real(sequence, "sampling_rate");
            if (!std::isfinite(sampling_rate)) {
                malformed("an Ogg packet sequence declares a non-finite sample rate");
            }
            if (sampling_rate <= 0.0) {
                malformed("an Ogg packet sequence declares a non-positive sample rate");
            }
            if (sampling_rate > static_cast<double>(max_supported_sample_rate)) {
                malformed("an Ogg packet sequence declares an out-of-range sample rate");
            }
            const resource_value &packet_data = require_property(sequence, "packet_data");
            if (packet_data.kind != value_kind::array) {
                malformed("an Ogg packet sequence has no page array");
            }
            if (packet_data.array.empty()) {
                malformed("an Ogg packet sequence has no pages");
            }
            const resource_value &granules = require_property(sequence, "granule_positions");
            if (granules.kind != value_kind::integer_array) {
                malformed("an Ogg packet sequence has no integer granule list");
            }
            if (granules.integer_array.size() != packet_data.array.size()) {
                malformed("an Ogg packet sequence granule count does not match its page count");
            }

            std::vector<codecs::ogg_packet_view> packets;
            std::uint64_t total_bytes = 0;
            std::int64_t previous_granule = -1;
            for (std::size_t page_index = 0; page_index < packet_data.array.size(); ++page_index) {
                const resource_value &page = packet_data.array[page_index];
                if (page.kind != value_kind::array) {
                    malformed("an Ogg packet sequence page is not an array");
                }
                if (page.array.empty()) {
                    malformed("an Ogg packet sequence has an empty page");
                }
                const std::int64_t granule = granules.integer_array[page_index];
                if (granule < -1) {
                    malformed("an Ogg page granule is below -1");
                }
                // The first page opens the logical stream, so its granule must be the transport's
                // beginning-of-stream value (unspecified or zero); the muxer rejects a positive
                // first packet granule, so the shared resolution rejects a positive first page here.
                if (page_index == 0 && granule > 0) {
                    malformed("the first Ogg page declares a positive granule");
                }
                if (granule >= 0 && previous_granule >= 0 && granule < previous_granule) {
                    malformed("Ogg page granules are not monotonic");
                }
                if (granule >= 0) {
                    previous_granule = granule;
                }
                for (std::size_t packet_index = 0; packet_index < page.array.size(); ++packet_index) {
                    const resource_value &packet = page.array[packet_index];
                    if (packet.kind != value_kind::bytes) {
                        malformed("an Ogg page carries a non-byte packet");
                    }
                    codecs::ogg_packet_view view;
                    view.data = as_span(packet.bytes);
                    view.granule_position = packet_index + 1 == page.array.size() ? granule : -1;
                    packets.push_back(view);
                }
            }

            if (packets.size() < 3) {
                malformed("an Ogg packet sequence has fewer than three Vorbis headers");
            }
            for (const resource_value &page : packet_data.array) {
                for (const resource_value &packet : page.array) {
                    const std::uint64_t length = stored_byte_length(packet, metadata_only);
                    if (length > limits.max_input_bytes - total_bytes) {
                        malformed("an Ogg packet sequence exceeds the input byte budget");
                    }
                    total_bytes += length;
                }
            }

            check_vorbis_header(packets[0].data, 0x01U);
            check_vorbis_header(packets[1].data, 0x03U);
            check_vorbis_header(packets[2].data, 0x05U);
            check_vorbis_identification(packets[0].data, sampling_rate);

            resolved_audio result;
            result.container = audio_container::ogg;
            result.packets = std::move(packets);
            return result;
        }

        std::optional<resolved_audio> resolve_mp3(const resource_record &record, const codecs::audio_limits &limits,
                                                  const bool metadata_only)
        {
            const resource_value &data = require_bytes(record, "data");
            const std::uint64_t length = stored_byte_length(data, metadata_only);
            check_input_budget(length, limits);
            check_output_budget(length, limits);
            const auto frame_at = [&](const std::size_t offset) {
                if (data.bytes.size() < offset + 4) {
                    return false;
                }
                const unsigned first = byte_value(data.bytes[offset]);
                const unsigned second = byte_value(data.bytes[offset + 1]);
                const unsigned third = byte_value(data.bytes[offset + 2]);
                return first == 0xFFU && (second & 0xE6U) == 0xE2U && (third >> 4U) != 0xFU &&
                       ((third >> 2U) & 0x3U) != 0x3U;
            };
            const bool leading_two = !frame_at(0) && frame_at(2);
            // The private dr_mp3 adapter (mp3_codec.h) performs the bounded recognition: an
            // optional ID3v2 tag followed by a complete Layer III frame, without decoding PCM.
            if (metadata_only) {
                if (data.bytes.size() < 4) {
                    malformed("an MP3 payload has no captured frame header");
                }
                // One imported Godot MP3 in the corpus carries two leading bytes before a valid
                // Layer III frame. Typed AudioStreamMP3 pass-through preserves those bytes; the
                // desktop decoder independently accepts the resulting file.
                const bool mpeg_layer3 = frame_at(0) || frame_at(2);
                const bool id3 = matches(as_span(data.bytes), "ID3", 3) && length >= 10;
                if (!mpeg_layer3 && !id3) {
                    malformed("an MP3 payload has no capture header");
                }
            } else {
                const std::span<const std::byte> bytes = as_span(data.bytes);
                static_cast<void>(codecs::probe_mp3(leading_two ? bytes.subspan(2) : bytes, limits));
            }
            resolved_audio result;
            result.container = audio_container::mp3;
            result.embedded = as_span(data.bytes);
            result.output_size = length;
            result.mp3_leading_bytes = leading_two ? 2 : 0;
            return result;
        }

        std::optional<resolved_audio> resolve_audio(const binary_resource &resource, const codecs::audio_limits &limits,
                                                    const bool metadata_only)
        {
            if (resource.records.empty()) {
                return std::nullopt;
            }
            const resource_record &root = resource.records.back();
            const std::string_view type = root.type;
            if (!supports_audio_resource_class(type)) {
                return std::nullopt;
            }
            if (resource.main_type != root.type) {
                malformed("the root record disagrees with the declared main type");
            }
            if (type == "Sample") {
                return resolve_sample_record(root, limits, metadata_only);
            }
            if (type == "AudioStreamMP3") {
                return resolve_mp3(root, limits, metadata_only);
            }
            if (type == "AudioStreamOGGVorbis") {
                return resolve_ogg_bytes(root, limits, metadata_only);
            }
            if (type == "AudioStreamOggVorbis") {
                const resource_value &reference = require_property(root, "packet_sequence");
                if (reference.kind != value_kind::reference) {
                    malformed("a packet_sequence field is not a resource reference");
                }
                return resolve_packet_sequence(resource, reference.reference, limits, metadata_only);
            }
            return resolve_wave_record(root, resource.engine.major, limits, metadata_only);
        }

        codecs::pcm_audio decode_wave(const resolved_audio &resolved, const codecs::audio_limits &limits)
        {
            if (resolved.encoding == wav_encoding::ima) {
                // A Godot 2 Sample declares its exact positive frame count, which may omit the single
                // trailing padding nibble the writer emitted; the declared-count overload decodes
                // exactly that count, so the padding nibble is neither decoded nor allocated. The
                // Godot 3/4 sample classes carry no declared count, so the whole-payload overload
                // decodes every encoded frame.
                if (resolved.sample_frames == 0) {
                    return codecs::decode_godot_ima(resolved.payload, resolved.channels, resolved.sample_rate, limits);
                }
                return codecs::decode_godot_ima(resolved.payload, resolved.channels, resolved.sample_rate,
                                                resolved.sample_frames, limits);
            }
            if (resolved.encoding == wav_encoding::qoa) {
                return codecs::decode_qoa(resolved.payload, limits);
            }

            codecs::pcm_audio pcm;
            pcm.sample_rate = resolved.sample_rate;
            pcm.channels = resolved.channels;
            if (resolved.encoding == wav_encoding::pcm16) {
                pcm.samples.resize(resolved.payload.size() / 2);
                for (std::size_t index = 0; index < pcm.samples.size(); ++index) {
                    const auto low = std::to_integer<std::uint16_t>(resolved.payload[index * 2]);
                    const auto high = std::to_integer<std::uint32_t>(resolved.payload[index * 2 + 1]);
                    pcm.samples[index] = static_cast<std::int16_t>(static_cast<std::uint16_t>(low | (high << 8U)));
                }
                return pcm;
            }
            pcm.samples.reserve(resolved.payload.size());
            for (const std::byte value : resolved.payload) {
                const auto signed_byte = static_cast<std::int8_t>(std::to_integer<std::uint8_t>(value));
                pcm.samples.push_back(static_cast<std::int16_t>(static_cast<std::int32_t>(signed_byte) * 256));
            }
            return pcm;
        }
    } // namespace

    std::optional<audio_resource_info> inspect_audio_resource(const binary_resource &resource,
                                                              const codecs::audio_limits &limits)
    {
        const auto resolved = resolve_audio(resource, limits, false);
        if (!resolved.has_value()) {
            return std::nullopt;
        }
        if (resolved->container == audio_container::wav) {
            return audio_resource_info{"wav", resolved->output_size};
        }
        if (resolved->container == audio_container::mp3) {
            return audio_resource_info{"mp3", resolved->output_size};
        }
        if (resolved->packets.empty()) {
            return audio_resource_info{"ogg", resolved->output_size};
        }
        return audio_resource_info{"ogg", std::nullopt};
    }

    std::optional<audio_resource_info> inspect_audio_resource_metadata(const binary_resource &resource,
                                                                       const codecs::audio_limits &limits)
    {
        const auto resolved = resolve_audio(resource, limits, true);
        if (!resolved.has_value()) {
            return std::nullopt;
        }
        if (resolved->container == audio_container::wav) {
            return audio_resource_info{"wav", resolved->output_size};
        }
        if (resolved->container == audio_container::mp3) {
            return audio_resource_info{"mp3", resolved->output_size};
        }
        return audio_resource_info{
            "ogg", resolved->packets.empty() ? std::optional<std::uint64_t>(resolved->output_size) : std::nullopt};
    }

    std::optional<std::string_view> audio_resource_extension(const binary_resource &resource,
                                                             const codecs::audio_limits &limits)
    {
        const auto info = inspect_audio_resource(resource, limits);
        if (!info.has_value()) {
            return std::nullopt;
        }
        return info->extension;
    }

    void write_audio_resource(const binary_resource &resource,
                              const std::function<void(std::span<const std::byte>)> &sink,
                              const codecs::audio_limits &limits)
    {
        const std::optional<resolved_audio> resolved = resolve_audio(resource, limits, false);
        if (!resolved.has_value()) {
            malformed("the resource is not a supported audio stream");
        }
        if (resolved->container == audio_container::mp3) {
            sink(resolved->embedded);
            return;
        }
        if (resolved->container == audio_container::ogg) {
            if (resolved->packets.empty()) {
                sink(resolved->embedded);
                return;
            }
            codecs::ogg_limits ogg_limits;
            ogg_limits.max_input_bytes = limits.max_input_bytes;
            ogg_limits.max_output_bytes = limits.max_output_bytes;
            codecs::write_ogg(resolved->packets, sink, ogg_limits);
            return;
        }
        codecs::write_wav(decode_wave(*resolved, limits), sink, limits);
    }

    void write_audio_resource_streaming(const binary_resource &metadata, const io::byte_source &source,
                                        const io::byte_sink &sink, const codecs::audio_limits &limits)
    {
        const std::optional<resolved_audio> resolved = resolve_audio(metadata, limits, true);
        if (!resolved.has_value()) {
            malformed("the resource is not a supported audio stream");
        }
        const resource_record &root = metadata.records.back();
        if (root.type == "AudioStreamOggVorbis") {
            const auto &reference = require_property(root, "packet_sequence").reference;
            const resource_record &sequence = metadata.records[reference.index];
            const auto &pages = require_property(sequence, "packet_data").array;
            const auto &granules = require_property(sequence, "granule_positions").integer_array;
            std::vector<codecs::ogg_packet_location> locations;
            std::vector<const resource_value *> values;
            for (std::size_t page_index = 0; page_index < pages.size(); ++page_index) {
                const auto &packets = pages[page_index].array;
                for (std::size_t packet_index = 0; packet_index < packets.size(); ++packet_index) {
                    const resource_value &packet = packets[packet_index];
                    const std::uint64_t length = stored_byte_length(packet, true);
                    if (packet.byte_offset > source.size || length > source.size - packet.byte_offset) {
                        malformed("an Ogg packet exceeds the source extent");
                    }
                    locations.push_back({length, packet_index + 1 == packets.size() ? granules[page_index] : -1});
                    values.push_back(&packet);
                }
            }
            codecs::ogg_limits transport;
            transport.max_input_bytes = limits.max_input_bytes;
            transport.max_output_bytes = limits.max_output_bytes;
            codecs::write_ogg_paged(
                locations,
                [&](const std::size_t index, const std::uint64_t packet_offset,
                    const std::span<std::byte> destination) {
                    std::uint64_t copied = 0;
                    while (copied < destination.size()) {
                        const std::size_t take = static_cast<std::size_t>(
                            (std::min)(static_cast<std::uint64_t>(destination.size()) - copied, std::uint64_t{4096}));
                        source.read(values[index]->byte_offset + packet_offset + copied,
                                    destination.subspan(static_cast<std::size_t>(copied), take));
                        copied += take;
                    }
                },
                sink, transport);
            return;
        }
        const resource_value &data =
            root.type == "Sample" ? dict_bytes(require_dictionary(root, "data"), "data") : require_bytes(root, "data");
        const std::uint64_t length = stored_byte_length(data, true);
        if (data.byte_offset > source.size || length > source.size - data.byte_offset) {
            malformed("an audio payload exceeds the source extent");
        }
        if (resolved->container != audio_container::wav) {
            // Packet sequences returned above. Only raw MP3/Ogg streams reach this branch.
            if (resolved->container == audio_container::mp3) {
                const std::uint64_t skip = resolved->mp3_leading_bytes;
                const io::byte_source mp3_source{
                    length - skip,
                    [&](const std::uint64_t offset, const std::span<std::byte> output) {
                        source.read(data.byte_offset + skip + offset, output);
                    },
                };
                static_cast<void>(codecs::probe_mp3_source(mp3_source, limits));
            }
            io::copy_byte_range(source, data.byte_offset, length, sink);
            return;
        }
        const std::uint64_t frames = (resolved->output_size - 44) / (std::uint64_t{2} * resolved->channels);
        if (resolved->encoding == wav_encoding::ima) {
            const std::uint64_t seed_bytes = data.bytes.size() - resolved->payload.size();
            codecs::godot_ima_decoder_state state{};
            codecs::write_wav_streaming(
                frames, resolved->channels, resolved->sample_rate,
                [&](const std::uint64_t first_frame, const std::span<std::int16_t> samples) {
                    const std::size_t chunk_frames = samples.size() / resolved->channels;
                    const std::size_t input_count = ((chunk_frames + 1) / 2) * resolved->channels;
                    std::array<std::byte, 2048> packed{};
                    source.read(data.byte_offset + seed_bytes + first_frame / 2 * resolved->channels,
                                std::span<std::byte>(packed.data(), input_count));
                    codecs::decode_godot_ima_chunk(std::span<const std::byte>(packed.data(), input_count),
                                                   resolved->channels, chunk_frames, state, samples);
                },
                sink, limits);
            return;
        }
        if (resolved->encoding == wav_encoding::qoa) {
            struct qoa_scratch final
            {
                std::array<std::byte, 8192> encoded{};
                std::array<std::int16_t, 10240> decoded{};
            };
            auto scratch = std::make_unique<qoa_scratch>();
            auto &encoded = scratch->encoded;
            const std::span<std::int16_t> decoded = scratch->decoded;
            std::uint64_t cursor = 8;
            std::size_t available = 0;
            std::size_t consumed = 0;
            codecs::write_wav_streaming(
                frames, resolved->channels, resolved->sample_rate,
                [&](const std::uint64_t first_frame, const std::span<std::int16_t> samples) {
                    std::size_t copied = 0;
                    const std::size_t requested = samples.size() / resolved->channels;
                    while (copied < requested) {
                        if (consumed == available) {
                            if (length - cursor < 8) {
                                malformed("a QOA frame header is truncated");
                            }
                            source.read(data.byte_offset + cursor, std::span<std::byte>(encoded.data(), 8));
                            const std::size_t frame_size = read_u16_be(encoded.data() + 6);
                            if (frame_size > encoded.size() || frame_size > length - cursor || frame_size < 8) {
                                malformed("a QOA frame size exceeds the payload");
                            }
                            source.read(data.byte_offset + cursor, std::span<std::byte>(encoded.data(), frame_size));
                            available = codecs::decode_qoa_frame(std::span<const std::byte>(encoded.data(), frame_size),
                                                                 resolved->channels, resolved->sample_rate, decoded);
                            if (available > frames - first_frame - copied) {
                                malformed("a QOA frame exceeds the declared sample count");
                            }
                            consumed = 0;
                            cursor += frame_size;
                        }
                        const std::size_t take = (std::min)(requested - copied, available - consumed);
                        std::copy_n(decoded.data() + consumed * resolved->channels, take * resolved->channels,
                                    samples.data() + copied * resolved->channels);
                        copied += take;
                        consumed += take;
                    }
                },
                sink, limits);
            return;
        }
        codecs::write_wav_streaming(
            frames, resolved->channels, resolved->sample_rate,
            [&](const std::uint64_t first_frame, const std::span<std::int16_t> samples) {
                const std::uint64_t start = first_frame * pcm_frame_bytes(resolved->encoding, resolved->channels);
                const std::size_t input_count = samples.size() * (resolved->encoding == wav_encoding::pcm16 ? 2 : 1);
                std::array<std::byte, 4096> input{};
                for (std::size_t position = 0; position < input_count; position += input.size()) {
                    const std::size_t take = (std::min)(input_count - position, input.size());
                    source.read(data.byte_offset + start + position, std::span<std::byte>(input.data(), take));
                    if (resolved->encoding == wav_encoding::pcm8) {
                        for (std::size_t i = 0; i < take; ++i) {
                            const auto signed_byte = static_cast<std::int8_t>(std::to_integer<std::uint8_t>(input[i]));
                            samples[position + i] =
                                static_cast<std::int16_t>(static_cast<std::int32_t>(signed_byte) * 256);
                        }
                    } else {
                        for (std::size_t i = 0; i < take / 2; ++i) {
                            const auto low = std::to_integer<std::uint16_t>(input[2 * i]);
                            const auto high = std::to_integer<std::uint32_t>(input[2 * i + 1]);
                            samples[position / 2 + i] =
                                static_cast<std::int16_t>(static_cast<std::uint16_t>(low | (high << 8U)));
                        }
                    }
                }
            },
            sink, limits);
    }
    bool supports_audio_resource_class(const std::string_view class_name) noexcept
    {
        return class_name == "Sample" || class_name == "AudioStreamSample" || class_name == "AudioStreamWAV" ||
               class_name == "AudioStreamMP3" || class_name == "AudioStreamOGGVorbis" ||
               class_name == "AudioStreamOggVorbis";
    }
} // namespace unpackrat::godot
