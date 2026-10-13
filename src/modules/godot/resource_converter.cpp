#include "resource_converter.h"

#include "core/codecs/audio_types.h"
#include "core/codecs/image_types.h"
#include "core/godot/audio_resource.h"
#include "core/godot/binary_resource.h"
#include "core/godot/font_resource.h"
#include "core/godot/rscc.h"
#include "core/godot/texture.h"
#include "core/io/byte_source.h"
#include "image_resource.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <ios>
#include <istream>
#include <limits>
#include <optional>
#include <span>
#include <string>
#include <string_view>
#include <type_traits>
#include <utility>
#include <vector>

// Dispatches the physical resource families the module exposes as a converted view. The three
// recognisable containers are the two texture envelopes handled by ``texture.cpp`` and the binary
// "RSRC"/"RSCC" resources handled by ``binary_resource.cpp`` and ``rscc.cpp``; the actual pixel and
// audio conversion stays in the accepted ``image_resource``/``audio_resource`` adapters. Probing
// only inspects metadata; the accepted writers perform conversion during extraction.

namespace unpackrat::godot
{
    namespace
    {
        constexpr std::size_t copy_chunk_bytes = std::size_t{64} * 1024;

        [[noreturn]] void throw_source()
        {
            throw resource_source_error("godot resource source is not readable");
        }

        std::uint64_t stream_size(std::istream &stream)
        {
            try {
                stream.clear();
                const std::streamoff original = stream.tellg();
                stream.seekg(0, std::ios::end);
                const std::streamoff end = stream.tellg();
                stream.clear();
                stream.seekg(original);
                if (original < 0 || end < original) {
                    throw_source();
                }
                return static_cast<std::uint64_t>(end);
            } catch (const std::ios_base::failure &) {
                throw_source();
            }
        }

        // One operation owns every access through this borrowed stream. A known position is
        // reused only while our reader controls it; external probes and sinks invalidate it.
        class resource_reader final
        {
          public:
            explicit resource_reader(std::istream &stream) : stream_(stream)
            {
            }

            void invalidate() noexcept
            {
                next_.reset();
            }

            std::istream &borrow_stream() noexcept
            {
                invalidate();
                return stream_;
            }

            void read_at(const std::uint64_t offset, const std::span<std::byte> destination)
            {
                const bool sequential = next_.has_value() && *next_ == offset;
                invalidate();
                try {
                    stream_.clear();
                    if (!sequential) {
                        stream_.seekg(static_cast<std::streamoff>(offset));
                    }
                    if (!stream_) {
                        throw_source();
                    }
                    stream_.read(reinterpret_cast<char *>(destination.data()),
                                 static_cast<std::streamsize>(destination.size()));
                } catch (const std::ios_base::failure &) {
                    throw_source();
                }
                if (stream_.gcount() != static_cast<std::streamsize>(destination.size())) {
                    throw_source();
                }
                next_ = offset + destination.size();
            }

            void validate_source_window(const byte_range &source)
            {
                if (source.size > (std::numeric_limits<std::uint64_t>::max)() - source.offset) {
                    throw_source();
                }
                if (!size_.has_value()) {
                    invalidate();
                    size_ = stream_size(stream_);
                }
                if (source.offset + source.size > *size_) {
                    throw_source();
                }
            }

          private:
            std::istream &stream_;
            std::optional<std::uint64_t> next_;
            std::optional<std::uint64_t> size_;
        };
        bool magic_matches(const std::array<std::byte, 4> &magic, const char (&text)[5]) noexcept
        {
            for (std::size_t index = 0; index < magic.size(); ++index) {
                if (std::to_integer<char>(magic[index]) != text[index]) {
                    return false;
                }
            }
            return true;
        }

        enum class wire_kind : std::uint8_t
        {
            none,
            texture,
            rsrc,
            rscc,
        };

        wire_kind sniff(resource_reader &input, const byte_range &source)
        {
            std::array<std::byte, 4> magic{};
            if (source.size < magic.size()) {
                return wire_kind::none;
            }
            input.read_at(source.offset, magic);
            if (magic_matches(magic, "GDST") || magic_matches(magic, "GST2")) {
                return wire_kind::texture;
            }
            if (magic_matches(magic, "RSRC")) {
                return wire_kind::rsrc;
            }
            if (magic_matches(magic, "RSCC")) {
                return wire_kind::rscc;
            }
            return wire_kind::none;
        }

        std::string texture_extension(const texture_encoding encoding)
        {
            // The texture prober returns only these three encodings. PNG and raw both become PNG.
            return encoding == texture_encoding::webp ? ".webp" : ".png";
        }

        std::optional<resource_descriptor> descriptor_for(const binary_resource &resource)
        {
            try {
                const auto image = inspect_image_resource_metadata(resource, codecs::image_limits{});
                if (image.has_value()) {
                    return resource_descriptor{resource_kind::image, std::string(image->extension)};
                }
            } catch (const codecs::image_error &) {
                return std::nullopt;
            }
            try {
                const auto audio = inspect_audio_resource_metadata(resource, codecs::audio_limits{});
                if (audio.has_value()) {
                    return resource_descriptor{resource_kind::audio, "." + std::string(audio->extension)};
                }
            } catch (const codecs::audio_error &) {
                return std::nullopt;
            }
            try {
                const auto font = font_resource_extension(resource);
                if (font.has_value()) {
                    return resource_descriptor{resource_kind::font, "." + std::string(*font)};
                }
            } catch (const font_error &) {
                return std::nullopt;
            }
            return std::nullopt;
        }

        std::optional<resource_descriptor> probe_texture_resource(resource_reader &input, const byte_range &source)
        {
            try {
                const texture_payload payload = probe_texture(input.borrow_stream(), source, texture_limits{});
                return resource_descriptor{resource_kind::texture, texture_extension(payload.encoding)};
            } catch (const texture_source_error &) {
                throw_source();
            } catch (const texture_unsupported &) {
                // A structurally valid envelope carrying a recognised but unmodelled pixel family
                // keeps its raw original instead of failing the whole listing.
                return std::nullopt;
            } catch (const texture_error &) {
                return std::nullopt;
            }
        }

        template <typename Consumer>
        auto with_binary_metadata(resource_reader &input, const byte_range &source, const bool rsrc, Consumer &&consume)
        {
            input.validate_source_window(source);
            const auto read_stored = [&](const std::uint64_t offset, const std::span<std::byte> target) {
                // The binary parser and RSCC reader validate their logical ranges before
                // invoking this byte-source callback; the physical window is checked above.
                input.read_at(source.offset + offset, target);
            };
            std::optional<rscc_metadata_reader> compressed;
            io::byte_source logical{source.size, read_stored};
            if (!rsrc) {
                rscc_limits limits;
                limits.max_input_bytes = source.size;
                limits.max_output_bytes = (std::numeric_limits<std::uint32_t>::max)();
                limits.max_block_bytes = (std::numeric_limits<std::uint32_t>::max)();
                limits.max_blocks = source.size / 4;
                compressed.emplace(source.size, read_stored, limits);
                logical.size = compressed->logical_size();
                logical.read = [&compressed](const std::uint64_t offset, const std::span<std::byte> target) {
                    compressed->read(offset, target);
                };
            }
            resource_limits limits;
            limits.max_input_bytes = logical.size;
            using result_type = decltype(consume(std::declval<const binary_resource &>(), logical));
            if constexpr (std::is_void_v<result_type>) {
                const binary_resource metadata =
                    read_binary_resource_metadata(logical.size, logical.read, rsrc, limits);
                consume(metadata, logical);
                if (compressed.has_value()) {
                    compressed->validate_terminal_block();
                }
            } else {
                const auto metadata = read_binary_resource_metadata_if(
                    logical.size, logical.read,
                    [](const std::string_view type) {
                        return supports_image_resource_class(type) || supports_audio_resource_class(type) ||
                               supports_font_resource_class(type);
                    },
                    rsrc, limits);
                if (!metadata.has_value()) {
                    return result_type{};
                }
                return consume(*metadata, logical);
            }
        }

        std::optional<resource_descriptor> classify_binary(resource_reader &input, const byte_range &source,
                                                           const bool rsrc)
        {
            try {
                return with_binary_metadata(
                    input, source, rsrc,
                    [](const binary_resource &metadata, const io::byte_source &) { return descriptor_for(metadata); });
            } catch (const rscc_error &) {
                return std::nullopt;
            } catch (const resource_error &) {
                return std::nullopt;
            }
        }

        void stream_range(resource_reader &input, const byte_range &range,
                          const std::function<void(std::span<const std::byte>)> &sink)
        {
            std::uint64_t offset = range.offset;
            std::uint64_t remaining = range.size;
            std::vector<std::byte> buffer;
            while (remaining > 0) {
                const std::uint64_t chunk =
                    remaining < copy_chunk_bytes ? remaining : static_cast<std::uint64_t>(copy_chunk_bytes);
                buffer.resize(static_cast<std::size_t>(chunk));
                input.read_at(offset, buffer);
                sink(std::span<const std::byte>(buffer.data(), static_cast<std::size_t>(chunk)));
                offset += chunk;
                remaining -= chunk;
            }
        }

        void convert_texture(resource_reader &input, const byte_range &source,
                             const std::function<void(std::span<const std::byte>)> &sink)
        {
            texture_payload payload;
            try {
                payload = probe_texture(input.borrow_stream(), source, texture_limits{});
            } catch (const texture_source_error &) {
                throw_source();
            }
            if (payload.encoding != texture_encoding::raw) {
                stream_range(input, payload.body, sink);
                return;
            }
            const io::byte_source base{payload.body.size,
                                       [&](const std::uint64_t offset, const std::span<std::byte> target) {
                                           // The row writer validates the exact mip geometry and reads only its rows.
                                           input.read_at(payload.body.offset + offset, target);
                                       }};
            write_raw_image_streaming(base, payload.engine_major, payload.wire_image_format, payload.width,
                                      payload.height, sink, codecs::image_limits{});
        }

        void convert_binary(resource_reader &input, const byte_range &source, const resource_descriptor &descriptor,
                            const std::function<void(std::span<const std::byte>)> &sink)
        {
            input.validate_source_window(source);
            const wire_kind kind = sniff(input, source);
            if (kind != wire_kind::rsrc && kind != wire_kind::rscc) {
                throw resource_error("godot converted source is not a binary resource");
            }
            with_binary_metadata(
                input, source, kind == wire_kind::rsrc,
                [&](const binary_resource &metadata, const io::byte_source &logical) {
                    if (descriptor.kind == resource_kind::font) {
                        const auto info = inspect_font_resource(metadata);
                        if (!info.has_value() || descriptor.extension != "." + info->extension) {
                            throw font_error("godot font source no longer matches its descriptor");
                        }
                        io::copy_byte_range(logical, info->byte_offset, info->byte_length, sink);
                        return;
                    }
                    if (descriptor.kind == resource_kind::audio) {
                        const auto info = inspect_audio_resource_metadata(metadata);
                        if (!info.has_value() || descriptor.extension != "." + std::string(info->extension)) {
                            throw codecs::audio_error("godot audio source no longer matches its descriptor");
                        }
                        write_audio_resource_streaming(metadata, logical, sink);
                        return;
                    }
                    const auto image = inspect_image_resource_metadata(metadata, codecs::image_limits{});
                    if (!image.has_value() || descriptor.extension != image->extension) {
                        throw codecs::image_error("godot image source no longer matches its descriptor");
                    }
                    write_image_resource_streaming(metadata, logical, sink, codecs::image_limits{});
                });
        }
    } // namespace

    std::optional<resource_descriptor> probe_resource(std::istream &stream, const byte_range source)
    {
        resource_reader input(stream);
        switch (sniff(input, source)) {
        case wire_kind::texture:
            return probe_texture_resource(input, source);
        case wire_kind::rsrc:
            return classify_binary(input, source, true);
        case wire_kind::rscc:
            return classify_binary(input, source, false);
        case wire_kind::none:
            break;
        }
        return std::nullopt;
    }

    void write_resource(std::istream &stream, const byte_range source, const resource_descriptor &descriptor,
                        const std::function<void(std::span<const std::byte>)> &sink)
    {
        resource_reader input(stream);
        const auto guarded_sink = [&](const std::span<const std::byte> bytes) {
            input.invalidate();
            sink(bytes);
        };
        if (descriptor.kind == resource_kind::texture) {
            convert_texture(input, source, guarded_sink);
            return;
        }
        convert_binary(input, source, descriptor, guarded_sink);
    }
} // namespace unpackrat::godot
