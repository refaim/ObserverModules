// Focused behavioral tests for the Godot resource dispatcher declared by
// ``src/modules/godot/resource_converter.h`` (namespace ``unpackrat::godot``). The header is
// included below; these tests state the observable resource-view contract.
//
// Contract under test:
//   enum class resource_kind { texture, image, audio };
//   struct resource_descriptor { resource_kind kind; std::string extension; };
//   class resource_source_error final : public std::runtime_error { ... };
//   std::optional<resource_descriptor> probe_resource(std::istream&, byte_range);
//   void write_resource(std::istream&, byte_range, const resource_descriptor&,
//                       const std::function<void(std::span<const std::byte>)>&);
//
// The dispatcher turns one stored physical resource window into a convertible descriptor and
// streams the converted view. Probing only inspects metadata. A malformed but recognisable
// candidate is declined, a genuine source fault is reported as ``resource_source_error``, and a
// sink's own exception is never remapped.
//
// Every fixture is repository-owned and generated in memory. The header geometry comes from the
// pinned official Godot sources already documented by ``texture.h``, ``rscc.h`` and
// ``binary_resource.h``; no archive byte or engine artefact is read.

#include "../../core/godot/audio_resource.h"
#include "../../core/godot/font_resource.h"
#include "../../core/godot/pck.h"
#include "../../core/godot/rscc.h"
#include "../../modules/godot/archive_session.h"
#include "../../modules/godot/image_resource.h"
#include "../../modules/godot/resource_converter.h"

#include "../support/compression_fixtures.h"
#include "../support/godot_resource_fixtures.h"
#include "../support/godot_texture_fixtures.h"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <functional>
#include <initializer_list>
#include <iterator>
#include <limits>
#include <optional>
#include <span>
#include <sstream>
#include <stdexcept>
#include <streambuf>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include <catch2/catch_test_macros.hpp>
#include <catch2/generators/catch_generators.hpp>

// The converted view is an independent PNG stream, so the tests decode it with libspng rather than
// trusting the converter for both sides. libspng is a static port, so its declarations must not
// carry the dllimport decoration; the macro stays private to this translation unit.
#define SPNG_STATIC
#include <spng.h>

namespace
{
    using test::support::build_gdst;
    using test::support::build_gst2;
    using test::support::build_resource;
    using test::support::build_rscc;
    using test::support::counting_stream;
    using test::support::gdst_fixture;
    using test::support::godot_bytes;
    using test::support::godot_resource_bytes;
    using test::support::gst2_fixture;
    using test::support::png_bytes;
    using test::support::resource_document_spec;
    using test::support::resource_property_spec;
    using test::support::resource_record_spec;
    using test::support::unreadable_stream;
    using test::support::variant_encoder;
    using test::support::webp_riff_payload;

    using unpackrat::codecs::compression_kind;
    using unpackrat::godot::byte_range;
    using unpackrat::godot::probe_resource;
    using unpackrat::godot::resource_descriptor;
    using unpackrat::godot::resource_kind;
    using unpackrat::godot::resource_source_error;
    using unpackrat::godot::write_resource;

    // Both fixture byte aliases are ``std::vector<std::uint8_t>``, so one converter serves them.
    std::string as_text(const std::vector<std::uint8_t> &bytes)
    {
        return std::string(reinterpret_cast<const char *>(bytes.data()), bytes.size());
    }

    std::string as_text(const std::vector<std::byte> &bytes)
    {
        return std::string(reinterpret_cast<const char *>(bytes.data()), bytes.size());
    }

    byte_range whole(const std::vector<std::uint8_t> &bytes)
    {
        return byte_range{0, static_cast<std::uint64_t>(bytes.size())};
    }

    byte_range whole(const std::vector<std::byte> &bytes)
    {
        return byte_range{0, static_cast<std::uint64_t>(bytes.size())};
    }

    std::vector<std::byte> as_byte_vector(const std::vector<std::uint8_t> &bytes)
    {
        std::vector<std::byte> result;
        result.reserve(bytes.size());
        for (const std::uint8_t value : bytes) {
            result.push_back(static_cast<std::byte>(value));
        }
        return result;
    }

    bool starts_with_png_signature(const std::span<const std::byte> stream)
    {
        constexpr std::byte signature[] = {std::byte{0x89}, std::byte{0x50}, std::byte{0x4E}, std::byte{0x47},
                                           std::byte{0x0D}, std::byte{0x0A}, std::byte{0x1A}, std::byte{0x0A}};
        if (stream.size() < std::size(signature)) {
            return false;
        }
        for (std::size_t index = 0; index < std::size(signature); ++index) {
            if (stream[index] != signature[index]) {
                return false;
            }
        }
        return true;
    }

    struct decoded_png final
    {
        std::uint32_t width = 0;
        std::uint32_t height = 0;
        std::vector<std::byte> pixels;
    };

    // Decodes a converted PNG stream into RGBA8 with libspng; the use is test-only and does not
    // exercise a production pixel path, so it independently judges the converter's raw output.
    decoded_png decode_png(const std::span<const std::byte> stream)
    {
        spng_ctx *context = spng_ctx_new(0);
        REQUIRE(context != nullptr);
        struct context_guard final
        {
            spng_ctx *context;
            ~context_guard()
            {
                spng_ctx_free(context);
            }
        } guard{context};

        REQUIRE(spng_set_png_buffer(context, stream.data(), stream.size()) == SPNG_OK);
        spng_ihdr header{};
        REQUIRE(spng_get_ihdr(context, &header) == SPNG_OK);
        std::size_t decoded_size = 0;
        REQUIRE(spng_decoded_image_size(context, SPNG_FMT_RGBA8, &decoded_size) == SPNG_OK);

        decoded_png result;
        result.width = header.width;
        result.height = header.height;
        result.pixels.resize(decoded_size);
        REQUIRE(spng_decode_image(context, result.pixels.data(), result.pixels.size(), SPNG_FMT_RGBA8, 0) == SPNG_OK);
        return result;
    }

    std::vector<std::byte> rgba(const std::initializer_list<unsigned int> values)
    {
        std::vector<std::byte> result;
        result.reserve(values.size());
        for (const unsigned int value : values) {
            result.push_back(static_cast<std::byte>(value));
        }
        return result;
    }

    std::vector<std::byte> write_collect(std::istream &stream, const byte_range source,
                                         const resource_descriptor &descriptor)
    {
        std::vector<std::byte> output;
        write_resource(stream, source, descriptor, [&output](const std::span<const std::byte> chunk) {
            output.insert(output.end(), chunk.begin(), chunk.end());
        });
        return output;
    }

    // A GDST (Godot 3 StreamTexture) carrying one or more preserved PNG levels.
    godot_bytes gdst_png_texture(const std::vector<godot_bytes> &mips)
    {
        gdst_fixture fixture;
        fixture.width = 8;
        fixture.height = 8;
        fixture.data_format = test::support::gdst_bit_png | test::support::gdst_bit_stream;
        fixture.mips = mips;
        return build_gdst(fixture);
    }

    godot_bytes gdst_webp_texture(const godot_bytes &riff)
    {
        godot_bytes wrapped = {'W', 'E', 'B', 'P'};
        wrapped.insert(wrapped.end(), riff.begin(), riff.end());
        gdst_fixture fixture;
        fixture.width = 8;
        fixture.height = 8;
        fixture.data_format = test::support::gdst_bit_webp | test::support::gdst_bit_stream;
        fixture.mips = {wrapped};
        return build_gdst(fixture);
    }

    godot_bytes gst2_png_texture(const std::vector<godot_bytes> &mips)
    {
        gst2_fixture fixture;
        fixture.display_width = 64;
        fixture.display_height = 64;
        fixture.image_data_format = test::support::gst2_data_png;
        fixture.image_width = 8;
        fixture.image_height = 8;
        fixture.image_format = test::support::image_format_rgba8;
        fixture.mips = mips;
        return build_gst2(fixture);
    }

    // A GST2 (Godot 4 CompressedTexture2D) preserving a plain RIFF/WebP payload. Unlike the
    // Godot 3 GDST container, Godot 4 writes the standard RIFF bytes without the "WEBP" prefix.
    godot_bytes gst2_webp_texture(const godot_bytes &riff)
    {
        gst2_fixture fixture;
        fixture.display_width = 64;
        fixture.display_height = 64;
        fixture.image_data_format = test::support::gst2_data_webp;
        fixture.image_width = 8;
        fixture.image_height = 8;
        fixture.image_format = test::support::image_format_rgba8;
        fixture.mips = {riff};
        return build_gst2(fixture);
    }

    godot_bytes gdst_raw_texture()
    {
        gdst_fixture fixture;
        fixture.width = 1;
        fixture.height = 1;
        fixture.data_format = test::support::image_format_rgba8;
        fixture.raw_data = godot_bytes{1, 2, 3, 4};
        return build_gdst(fixture);
    }

    // --- Godot binary resource builders (a subset of the exported fixture API) ---------------

    resource_property_spec property(std::string name, godot_resource_bytes value)
    {
        resource_property_spec spec;
        spec.name = std::move(name);
        spec.value = std::move(value);
        return spec;
    }

    resource_record_spec record(std::string path, std::string type, std::vector<resource_property_spec> properties)
    {
        resource_record_spec spec;
        spec.path = std::move(path);
        spec.type = std::move(type);
        spec.properties = std::move(properties);
        return spec;
    }

    template <typename Encode> godot_resource_bytes encode(const bool modern, Encode step)
    {
        test::support::wire_writer writer;
        variant_encoder encoder(writer, modern, false);
        step(encoder);
        return writer.take();
    }

    godot_resource_bytes vstring(const bool modern, const std::string_view value)
    {
        return encode(modern, [&](variant_encoder &encoder) { encoder.string(value); });
    }

    godot_resource_bytes vint(const bool modern, const std::int32_t value)
    {
        return encode(modern, [&](variant_encoder &encoder) { encoder.int32(value); });
    }

    godot_resource_bytes vbool(const bool modern, const bool value)
    {
        return encode(modern, [&](variant_encoder &encoder) { encoder.boolean(value); });
    }

    godot_resource_bytes vbytes(const bool modern, const std::span<const std::uint8_t> value)
    {
        return encode(modern, [&](variant_encoder &encoder) { encoder.bytes(value); });
    }

    void append_u32(std::vector<std::byte> &out, const std::uint32_t value)
    {
        for (unsigned shift = 0; shift < 32; shift += 8) {
            out.push_back(static_cast<std::byte>(value >> shift));
        }
    }

    // The Image "data" dictionary exactly as the engine writes it.
    godot_resource_bytes image_data(const std::uint32_t width, const std::uint32_t height,
                                    const std::string_view format, const std::span<const std::uint8_t> data)
    {
        const bool modern = true;
        const std::vector<godot_resource_bytes> fields{
            vstring(modern, "width"),   vint(modern, static_cast<std::int32_t>(width)),
            vstring(modern, "height"),  vint(modern, static_cast<std::int32_t>(height)),
            vstring(modern, "format"),  vstring(modern, format),
            vstring(modern, "mipmaps"), vbool(modern, false),
            vstring(modern, "data"),    vbytes(modern, data)};
        return encode(modern, [&](variant_encoder &encoder) { encoder.dictionary(fields); });
    }

    resource_document_spec document(const std::uint32_t engine_major, const std::uint32_t format_version)
    {
        resource_document_spec spec;
        spec.engine_major = engine_major;
        spec.engine_minor = engine_major >= 4 ? 5U : 6U;
        spec.format_version = format_version;
        if (engine_major == 2) {
            // A legacy ImageTexture names its "image" property by string-table index, so the
            // canonical Godot 2 document must declare that entry (mirrors godot_image_resource.cpp).
            spec.string_table.push_back("image");
        }
        return spec;
    }

    // A Godot 3/4 ImageTexture whose "image" property references an internal raw Image record.
    godot_resource_bytes modern_image_resource(const std::uint32_t major, const std::uint32_t format_version,
                                               const std::uint32_t width, const std::uint32_t height,
                                               const std::span<const std::uint8_t> data)
    {
        const bool modern = true;
        resource_document_spec spec = document(major, format_version);
        spec.records.push_back(
            record("local://0", "Image", {property("data", image_data(width, height, "RGBA8", data))}));
        spec.records.push_back(
            record("res://tex", "ImageTexture",
                   {property("image", encode(modern, [](variant_encoder &encoder) { encoder.object_internal(0); }))}));
        spec.main_type = spec.records.back().type; // "ImageTexture"
        return build_resource(spec);
    }

    // A Godot 2 ImageTexture carrying a preserved encoded Image variant ("PNG "/"WEBP" wrapped).
    godot_resource_bytes g2_encoded_image_resource(const std::uint32_t encoding,
                                                   const std::span<const std::uint8_t> data)
    {
        resource_document_spec spec = document(2, 1);
        spec.engine_minor = 1;
        spec.records.push_back(
            record("res://tex", "ImageTexture", {property("image", encode(false, [&](variant_encoder &encoder) {
                                                              encoder.image_encoded(encoding, data);
                                                          }))}));
        spec.main_type = spec.records.back().type;
        return build_resource(spec);
    }

    // A structurally valid but non-image resource: the same container with an unrelated root.
    godot_resource_bytes non_image_resource()
    {
        resource_document_spec spec = document(4, 6);
        spec.records.push_back(record("res://root", "Node", {}));
        spec.main_type = "Resource";
        return build_resource(spec);
    }

    std::vector<std::uint8_t> zero_bytes(const std::size_t count)
    {
        return std::vector<std::uint8_t>(count, 0);
    }

    // A virtual stream that holds one small in-memory buffer but reports an independent logical
    // size, and records the largest single fetch it is ever asked for. It lets a test drive the
    // dispatcher's window validation at its physical and policy boundaries without allocating the
    // large buffer a hostile window declares, and proves how much was read.
    class reporting_streambuf final : public std::streambuf
    {
      public:
        reporting_streambuf(std::string data, const std::uint64_t reported_size, const bool zero_extend = false,
                            const std::streamsize maximum_request = (std::numeric_limits<std::streamsize>::max)())
            : data_(std::move(data)), reported_size_(reported_size), zero_extend_(zero_extend),
              maximum_request_(maximum_request)
        {
        }

        [[nodiscard]] std::streamsize largest_request() const noexcept
        {
            return largest_request_;
        }

      protected:
        int_type underflow() override
        {
            return position_ < data_.size() ? traits_type::to_int_type(data_[position_]) : traits_type::eof();
        }

        int_type uflow() override
        {
            if (position_ >= data_.size()) {
                return traits_type::eof();
            }
            return traits_type::to_int_type(data_[position_++]);
        }

        std::streamsize xsgetn(char_type *target, const std::streamsize count) override
        {
            if (count > largest_request_) {
                largest_request_ = count;
            }
            if (count > maximum_request_) {
                return 0;
            }
            if (zero_extend_) {
                const std::uint64_t available = reported_size_ - position_;
                const std::uint64_t wanted = (std::min)(static_cast<std::uint64_t>(count), available);
                const std::uint64_t from_prefix =
                    position_ < data_.size() ? (std::min)(wanted, static_cast<std::uint64_t>(data_.size() - position_))
                                             : 0;
                if (from_prefix > 0) {
                    std::memcpy(target, data_.data() + position_, static_cast<std::size_t>(from_prefix));
                }
                if (wanted > from_prefix) {
                    std::memset(target + from_prefix, 0, static_cast<std::size_t>(wanted - from_prefix));
                }
                position_ += static_cast<std::size_t>(wanted);
                return static_cast<std::streamsize>(wanted);
            }
            const std::uint64_t available = position_ < data_.size() ? data_.size() - position_ : 0;
            const std::uint64_t wanted =
                static_cast<std::uint64_t>(count) < available ? static_cast<std::uint64_t>(count) : available;
            if (wanted > 0) {
                std::memcpy(target, data_.data() + position_, static_cast<std::size_t>(wanted));
                position_ += static_cast<std::size_t>(wanted);
            }
            return static_cast<std::streamsize>(wanted);
        }

        pos_type seekoff(const off_type offset, const std::ios_base::seekdir direction,
                         const std::ios_base::openmode mode) override
        {
            if ((static_cast<unsigned>(mode) & static_cast<unsigned>(std::ios_base::in)) == 0) {
                return pos_type(off_type(-1));
            }
            std::int64_t base = 0;
            if (direction == std::ios_base::cur) {
                base = static_cast<std::int64_t>(position_);
            } else if (direction == std::ios_base::end) {
                base = static_cast<std::int64_t>(reported_size_);
            }
            const std::int64_t target = base + offset;
            if (target < 0 || static_cast<std::uint64_t>(target) > reported_size_) {
                return pos_type(off_type(-1));
            }
            position_ = static_cast<std::size_t>(target);
            return pos_type(off_type(position_));
        }

        pos_type seekpos(const pos_type position, const std::ios_base::openmode mode) override
        {
            return seekoff(off_type(position), std::ios_base::beg, mode);
        }

      private:
        std::string data_;
        std::uint64_t reported_size_;
        std::size_t position_ = 0;
        std::streamsize largest_request_ = 0;
        bool zero_extend_ = false;
        std::streamsize maximum_request_ = (std::numeric_limits<std::streamsize>::max)();
    };

    class reporting_stream final : public std::istream
    {
      public:
        reporting_stream(std::string data, const std::uint64_t reported_size, const bool zero_extend = false,
                         const std::streamsize maximum_request = (std::numeric_limits<std::streamsize>::max)())
            : std::istream(nullptr), buffer_(std::move(data), reported_size, zero_extend, maximum_request)
        {
            rdbuf(&buffer_);
            clear();
        }

        [[nodiscard]] std::streamsize largest_request() const noexcept
        {
            return buffer_.largest_request();
        }

      private:
        reporting_streambuf buffer_;
    };

    // A configurable source stream that presents ``reported_size`` bytes at its end while holding
    // an independent in-memory prefix. It models the source faults the dispatcher must classify
    // rather than a declined container: a stream that cannot report its position, one whose end
    // precedes the position it already reported (a source that shrank), one whose seek raises the
    // stream exception, and one that runs dry partway through a read.
    class fault_streambuf final : public std::streambuf
    {
      public:
        fault_streambuf(std::string data, const std::uint64_t reported_size, const std::int64_t position)
            : data_(std::move(data)), size_(reported_size), position_(position)
        {
        }

        bool fail_current_seek = false;
        bool fail_end_seek = false;

      protected:
        int_type underflow() override
        {
            return traits_type::eof();
        }

        int_type uflow() override
        {
            return traits_type::eof();
        }

        std::streamsize xsgetn(char_type *target, const std::streamsize count) override
        {
            const std::uint64_t position = static_cast<std::uint64_t>(position_);
            const std::uint64_t available = position < size_ ? size_ - position : 0;
            const std::uint64_t wanted =
                static_cast<std::uint64_t>(count) < available ? static_cast<std::uint64_t>(count) : available;
            const std::uint64_t held = position < data_.size() ? data_.size() - position : 0;
            const std::uint64_t deliver = held < wanted ? held : wanted;
            if (deliver > 0) {
                std::memcpy(target, data_.data() + position, static_cast<std::size_t>(deliver));
            }
            position_ += static_cast<std::int64_t>(deliver);
            return static_cast<std::streamsize>(deliver);
        }

        pos_type seekoff(const off_type offset, const std::ios_base::seekdir direction,
                         const std::ios_base::openmode mode) override
        {
            if ((static_cast<unsigned>(mode) & static_cast<unsigned>(std::ios_base::in)) == 0) {
                return pos_type(off_type(-1));
            }
            if (direction == std::ios_base::cur && offset == 0) {
                return fail_current_seek ? pos_type(off_type(-1)) : pos_type(off_type(position_));
            }
            if (direction == std::ios_base::end && fail_end_seek) {
                return pos_type(off_type(-1));
            }
            std::int64_t base = 0;
            if (direction == std::ios_base::cur) {
                base = position_;
            } else if (direction == std::ios_base::end) {
                base = static_cast<std::int64_t>(size_);
            }
            const std::int64_t target = base + offset;
            if (target < 0 || static_cast<std::uint64_t>(target) > size_) {
                return pos_type(off_type(-1));
            }
            position_ = target;
            return pos_type(off_type(position_));
        }

        pos_type seekpos(const pos_type position, const std::ios_base::openmode mode) override
        {
            return seekoff(off_type(position), std::ios_base::beg, mode);
        }

      private:
        std::string data_;
        std::uint64_t size_;
        std::int64_t position_;
    };

    class fault_stream final : public std::istream
    {
      public:
        explicit fault_stream(std::string data, const std::uint64_t reported_size, const std::int64_t position = 0)
            : std::istream(nullptr), buffer_(std::move(data), reported_size, position)
        {
            rdbuf(&buffer_);
            clear();
        }

        [[nodiscard]] fault_streambuf &buffer() noexcept
        {
            return buffer_;
        }

      private:
        fault_streambuf buffer_;
    };

    // A Godot 4 ``AudioStreamWAV`` document whose record carries the canonical sample properties.
    godot_resource_bytes audio_wave_resource(const std::int64_t format, const std::int64_t mix_rate,
                                             const std::span<const std::uint8_t> data)
    {
        resource_document_spec spec = document(4, 6);
        spec.main_type = "AudioStreamWAV";
        spec.records.push_back(record("res://audio", "AudioStreamWAV",
                                      {property("data", vbytes(true, data)),
                                       property("format", vint(true, static_cast<std::int32_t>(format))),
                                       property("mix_rate", vint(true, static_cast<std::int32_t>(mix_rate))),
                                       property("stereo", vbool(true, false))}));
        return build_resource(spec);
    }
} // namespace

TEST_CASE("godot resource converter: probing raw texture leaves media bytes unread", "[godot][resource-converter]")
{
    gdst_fixture fixture;
    fixture.width = 64;
    fixture.height = 64;
    fixture.data_format = test::support::image_format_rgba8;
    fixture.raw_data = godot_bytes(std::size_t{64} * 64 * 4, 0x7F);
    const godot_bytes image = build_gdst(fixture);

    counting_stream stream(as_text(image));
    const auto descriptor = probe_resource(stream, whole(image));
    REQUIRE(descriptor.has_value());
    if (!descriptor.has_value()) {
        return;
    }
    CHECK(descriptor.value().extension == ".png");
    CHECK(stream.bytes_read() <= 64);
}

TEST_CASE("godot resource converter: raw texture extraction fetches bounded source ranges",
          "[godot][resource-converter][texture-stream]")
{
    gdst_fixture fixture;
    fixture.width = 64;
    fixture.height = 64;
    fixture.data_format = test::support::image_format_rgba8;
    fixture.raw_data = godot_bytes(std::size_t{64} * 64 * 4, 0x7F);
    const godot_bytes stored = build_gdst(fixture);
    reporting_stream stream(as_text(stored), stored.size(), true, 4096);
    const auto descriptor = probe_resource(stream, whole(stored));
    REQUIRE(descriptor.has_value());
    if (!descriptor.has_value()) {
        return;
    }
    std::vector<std::byte> converted;
    write_resource(stream, whole(stored), *descriptor, [&](const std::span<const std::byte> chunk) {
        converted.insert(converted.end(), chunk.begin(), chunk.end());
    });
    REQUIRE(starts_with_png_signature(converted));
    CHECK(stream.largest_request() <= 4096);
}

TEST_CASE("godot resource converter: binary image extraction fetches bounded source ranges",
          "[godot][resource-converter][image-stream]")
{
    const auto pixels = zero_bytes(std::size_t{64} * 64 * 4);
    const auto stored = modern_image_resource(4, 6, 64, 64, pixels);
    reporting_stream stream(as_text(stored), stored.size(), true, 4096);
    const auto descriptor = probe_resource(stream, whole(stored));
    REQUIRE(descriptor.has_value());
    if (!descriptor.has_value()) {
        return;
    }
    CHECK(descriptor.value().kind == resource_kind::image);
    std::vector<std::byte> converted;
    write_resource(stream, whole(stored), *descriptor, [&](const std::span<const std::byte> chunk) {
        converted.insert(converted.end(), chunk.begin(), chunk.end());
    });
    REQUIRE(starts_with_png_signature(converted));
    CHECK(stream.largest_request() <= 4096);
}

TEST_CASE("godot resource converter: embedded FontFile remains a byte-exact font view", "[godot][resource-converter]")
{
    const std::vector<std::uint8_t> font = {0x00, 0x01, 0x00, 0x00, 0, 0, 0, 0, 0, 0, 0, 0};
    auto spec = document(4, 6);
    spec.main_type = "FontFile";
    spec.records.push_back(record("res://font", "FontFile", {property("data/0", vbytes(true, font))}));
    const auto stored = build_resource(spec);

    std::istringstream stream(as_text(stored));
    const auto descriptor = probe_resource(stream, whole(stored));
    REQUIRE(descriptor.has_value());
    if (!descriptor.has_value()) {
        return;
    }
    CHECK(descriptor.value().kind == resource_kind::font);
    CHECK(descriptor.value().extension == ".ttf");

    std::istringstream writer(as_text(stored));
    CHECK(write_collect(writer, whole(stored), *descriptor) == as_byte_vector(font));
}

TEST_CASE("godot resource converter: a sparse oversized RSRC font is classified without reading its media body",
          "[godot][resource-converter][metadata-stream]")
{
    constexpr std::uint32_t declared_bytes = 270U * 1024U * 1024U;
    const std::vector<std::uint8_t> font = {0x00, 0x01, 0x00, 0x00, 0, 0, 0, 0, 0, 0, 0, 0};
    auto spec = document(4, 6);
    spec.main_type = "FontFile";
    spec.records.push_back(record("res://font", "FontFile", {property("data", vbytes(true, font))}));
    auto stored = build_resource(spec);
    REQUIRE(stored.size() >= font.size() + 4);
    const std::size_t length_at = stored.size() - font.size() - 4;
    REQUIRE(stored[length_at] == font.size());
    for (std::size_t index = 0; index < 4; ++index) {
        stored[length_at + index] = static_cast<std::uint8_t>(declared_bytes >> (index * 8));
    }
    const std::uint64_t physical_size = stored.size() + declared_bytes - font.size();
    reporting_stream stream(as_text(stored), physical_size, true, 4096);

    const auto descriptor = probe_resource(stream, byte_range{0, physical_size});
    REQUIRE(descriptor.has_value());
    if (!descriptor.has_value()) {
        return;
    }
    CHECK(descriptor.value().kind == resource_kind::font);
    CHECK(descriptor.value().extension == ".ttf");
    CHECK(stream.largest_request() <= 4096);

    std::uint64_t output_bytes = 0;
    bool chunks_small = true;
    write_resource(stream, byte_range{0, physical_size}, *descriptor, [&](const std::span<const std::byte> chunk) {
        chunks_small = chunks_small && chunk.size() <= 4096;
        if (output_bytes == 0) {
            REQUIRE(chunk.size() >= 4);
            CHECK(chunk[0] == std::byte{0});
            CHECK(chunk[1] == std::byte{1});
        }
        output_bytes += chunk.size();
    });
    CHECK(output_bytes == declared_bytes);
    CHECK(chunks_small);
    CHECK(stream.largest_request() <= 4096);
}

TEST_CASE("godot resource converter: a large RSCC font probes only metadata blocks",
          "[godot][resource-converter][metadata-stream]")
{
    constexpr std::uint32_t block_size = 1024U * 1024U;
    constexpr std::uint32_t declared_bytes = 270U * block_size;
    const std::vector<std::uint8_t> font = {0x00, 0x01, 0x00, 0x00, 0, 0, 0, 0, 0, 0, 0, 0};
    auto spec = document(4, 6);
    spec.include_magic = false;
    spec.main_type = "FontFile";
    spec.records.push_back(record("res://font", "FontFile", {property("data", vbytes(true, font))}));
    auto prefix = build_resource(spec);
    REQUIRE(prefix.size() >= font.size() + 4);
    const std::size_t length_at = prefix.size() - font.size() - 4;
    REQUIRE(prefix[length_at] == font.size());
    for (unsigned index = 0; index < 4; ++index) {
        prefix[length_at + index] = static_cast<std::uint8_t>(declared_bytes >> (index * 8));
    }
    const std::uint32_t logical_size = static_cast<std::uint32_t>(prefix.size() + declared_bytes - font.size());
    REQUIRE(prefix.size() < block_size);
    const std::uint32_t block_count = logical_size / block_size + 1;
    std::vector<std::byte> first(block_size, std::byte{0});
    for (std::size_t index = 0; index < prefix.size(); ++index) {
        first[index] = static_cast<std::byte>(prefix[index]);
    }
    const auto first_compressed = test::support::compress_block(first, compression_kind::deflate);
    const auto zero_compressed =
        test::support::compress_block(std::vector<std::byte>(block_size, std::byte{0}), compression_kind::deflate);
    const auto tail_compressed = test::support::compress_block(
        std::vector<std::byte>(logical_size % block_size, std::byte{0}), compression_kind::deflate);
    std::vector<std::byte> stored{std::byte{'R'}, std::byte{'S'}, std::byte{'C'}, std::byte{'C'}};
    append_u32(stored, static_cast<std::uint32_t>(compression_kind::deflate));
    append_u32(stored, block_size);
    append_u32(stored, logical_size);
    for (std::uint32_t index = 0; index < block_count; ++index) {
        const auto &compressed =
            index == 0 ? first_compressed : (index + 1 == block_count ? tail_compressed : zero_compressed);
        append_u32(stored, static_cast<std::uint32_t>(compressed.size()));
    }
    for (std::uint32_t index = 0; index < block_count; ++index) {
        const auto &compressed =
            index == 0 ? first_compressed : (index + 1 == block_count ? tail_compressed : zero_compressed);
        stored.insert(stored.end(), compressed.begin(), compressed.end());
    }
    stored.insert(stored.end(), {std::byte{'R'}, std::byte{'S'}, std::byte{'C'}, std::byte{'C'}});
    reporting_stream stream(as_text(stored), stored.size(), false, 4096);

    const auto descriptor = probe_resource(stream, whole(stored));
    REQUIRE(descriptor.has_value());
    if (!descriptor.has_value()) {
        return;
    }
    CHECK(descriptor.value().kind == resource_kind::font);
    CHECK(descriptor.value().extension == ".ttf");
    CHECK(stream.largest_request() <= 4096);

    std::uint64_t output_bytes = 0;
    bool chunks_small = true;
    write_resource(stream, whole(stored), *descriptor, [&](const std::span<const std::byte> chunk) {
        chunks_small = chunks_small && chunk.size() <= 4096;
        if (output_bytes == 0) {
            REQUIRE(chunk.size() >= 4);
            CHECK(chunk[0] == std::byte{0});
            CHECK(chunk[1] == std::byte{1});
        }
        output_bytes += chunk.size();
    });
    CHECK(output_bytes == declared_bytes);
    CHECK(chunks_small);
    CHECK(stream.largest_request() <= 4096);
}

TEST_CASE("godot resource converter: audio extraction reads a sparse PCM payload in bounded chunks",
          "[godot][resource-converter][audio-stream]")
{
    const std::uint32_t declared_bytes = GENERATE(1024U * 1024U, 270U * 1024U * 1024U);
    CAPTURE(declared_bytes);
    const std::vector<std::uint8_t> pcm = {0, 0};
    auto spec = document(4, 6);
    spec.main_type = "AudioStreamWAV";
    spec.records.push_back(record("res://audio", "AudioStreamWAV",
                                  {property("format", vint(true, 1)), property("mix_rate", vint(true, 8000)),
                                   property("stereo", vbool(true, false)), property("data", vbytes(true, pcm))}));
    auto stored = build_resource(spec);
    constexpr std::size_t original_padding = 2;
    REQUIRE(stored.size() >= pcm.size() + original_padding + 4);
    const std::size_t length_at = stored.size() - pcm.size() - original_padding - 4;
    REQUIRE(stored[length_at] == pcm.size());
    for (unsigned index = 0; index < 4; ++index)
        stored[length_at + index] = static_cast<std::uint8_t>(declared_bytes >> (index * 8));
    const std::uint64_t physical_size = stored.size() + declared_bytes - pcm.size() - original_padding;
    reporting_stream stream(as_text(stored), physical_size, true, 4096);
    const auto descriptor = probe_resource(stream, byte_range{0, physical_size});
    REQUIRE(descriptor.has_value());
    if (!descriptor.has_value()) {
        return;
    }
    CHECK(descriptor.value().kind == resource_kind::audio);
    CHECK(descriptor.value().extension == ".wav");
    std::uint64_t output_bytes = 0;
    write_resource(stream, byte_range{0, physical_size}, *descriptor,
                   [&](const std::span<const std::byte> chunk) { output_bytes += chunk.size(); });
    CHECK(output_bytes == declared_bytes + 44U);
    CHECK(stream.largest_request() <= 4096);
}

TEST_CASE("godot resource converter: corrupt zero-logical RSCC tail fails only during extraction",
          "[godot][resource-converter][terminal-block]")
{
    const std::vector<std::uint8_t> font = {0x00, 0x01, 0x00, 0x00, 0, 0, 0, 0, 0, 0, 0, 0};
    auto spec = document(4, 6);
    spec.include_magic = false;
    spec.main_type = "FontFile";
    spec.records.push_back(record("res://font", "FontFile", {property("data", vbytes(true, font))}));
    const auto logical = build_resource(spec);
    const auto stored_valid =
        build_rscc(as_byte_vector(logical), compression_kind::deflate, static_cast<std::uint32_t>(logical.size()));
    auto stored = stored_valid;
    REQUIRE(stored.size() >= 28);
    const std::uint32_t first_size =
        std::to_integer<std::uint32_t>(stored[16]) | std::to_integer<std::uint32_t>(stored[17]) << 8U |
        std::to_integer<std::uint32_t>(stored[18]) << 16U | std::to_integer<std::uint32_t>(stored[19]) << 24U;
    const std::size_t zero_block_at = 24 + first_size;
    REQUIRE(zero_block_at < stored.size() - 4);
    stored[zero_block_at] = std::byte{0xFF};
    std::istringstream stream(as_text(stored));
    const auto descriptor = probe_resource(stream, whole(stored));
    REQUIRE(descriptor.has_value());
    if (!descriptor.has_value()) {
        return;
    }
    CHECK(descriptor.value().kind == resource_kind::font);
    std::uint64_t written = 0;
    CHECK_THROWS_AS(write_resource(stream, whole(stored), *descriptor,
                                   [&](const std::span<const std::byte> bytes) { written += bytes.size(); }),
                    unpackrat::godot::rscc_error);
}
TEST_CASE("godot resource converter: a wide RSCC block yields font metadata without whole-block allocation",
          "[godot][resource-converter][wide-rscc]")
{
    const auto kind = GENERATE(compression_kind::fastlz, compression_kind::deflate, compression_kind::zstd,
                               compression_kind::gzip, compression_kind::brotli);
    CAPTURE(kind);
    constexpr std::size_t block_size = std::size_t{17} * 1024 * 1024;
    const std::vector<std::uint8_t> font = {0x00, 0x01, 0x00, 0x00, 0, 0, 0, 0, 0, 0, 0, 0};
    auto spec = document(4, 6);
    spec.include_magic = false;
    spec.main_type = "FontFile";
    spec.records.push_back(record("res://font", "FontFile", {property("data", vbytes(true, font))}));
    auto logical = build_resource(spec);
    REQUIRE(logical.size() > font.size() + 4);
    const std::size_t count_at = logical.size() - font.size() - 4;
    const std::uint32_t declared =
        static_cast<std::uint32_t>((block_size - logical.size() + font.size()) & ~std::size_t{3});
    for (unsigned index = 0; index < 4; ++index) {
        logical[count_at + index] = static_cast<std::uint8_t>(declared >> (index * 8));
    }
    logical.resize(block_size, 0);
    const auto stored = build_rscc(as_byte_vector(logical), kind, static_cast<std::uint32_t>(block_size));
    reporting_stream stream(as_text(stored), stored.size(), false, std::streamsize{64} * 1024);

    const auto descriptor = probe_resource(stream, whole(stored));
    REQUIRE(descriptor.has_value());
    if (!descriptor.has_value()) {
        return;
    }
    CHECK(descriptor.value().kind == resource_kind::font);
    CHECK(descriptor.value().extension == ".ttf");
    std::uint64_t output_bytes = 0;
    bool chunks_small = true;
    write_resource(stream, whole(stored), *descriptor, [&](const std::span<const std::byte> chunk) {
        chunks_small = chunks_small && chunk.size() <= 4096;
        output_bytes += chunk.size();
    });
    CHECK(output_bytes == declared);
    CHECK(chunks_small);
    CHECK(stream.largest_request() <= std::streamsize{64} * 1024);
}

TEST_CASE("godot resource converter: generated compatibility pack exposes legacy Sample and prefixed MP3",
          "[godot][resource-converter][media-compat]")
{
    const auto path =
        std::filesystem::path(__FILE__).parent_path().parent_path() / "data" / "godot" / "media-compat.pck";
    unpackrat::godot::archive_session session(path);
    const auto &entries = session.index().entries;
    REQUIRE(entries.size() == 2);
    CHECK(entries[0].path == "audio/legacy-negative-main.sample");
    CHECK(entries[1].path == "audio/prefixed.mp3");
    const auto sample = probe_resource(session.stream(), entries[0].body);
    REQUIRE(sample.has_value());
    if (!sample.has_value()) {
        return;
    }
    CHECK(sample.value().kind == resource_kind::audio);
    CHECK(sample.value().extension == ".wav");
    CHECK(write_collect(session.stream(), entries[0].body, *sample).size() == 52);
    const auto mp3 = probe_resource(session.stream(), entries[1].body);
    REQUIRE(mp3.has_value());
    if (!mp3.has_value()) {
        return;
    }
    CHECK(mp3.value().kind == resource_kind::audio);
    CHECK(mp3.value().extension == ".mp3");
    const auto output = write_collect(session.stream(), entries[1].body, *mp3);
    REQUIRE(output.size() == 1670);
    CHECK(output[0] == std::byte{0x89});
    CHECK(output[1] == std::byte{0x06});
    CHECK(output[2] == std::byte{0xFF});
}
TEST_CASE("godot resource converter: session probes both offset media members in the minimal pack",
          "[godot][resource-converter][session-offset]")
{
    const auto path =
        std::filesystem::path(__FILE__).parent_path().parent_path() / "data" / "godot" / "audio-font-minimal.pck";
    unpackrat::godot::archive_session session(path);
    const auto &entries = session.index().entries;
    REQUIRE(entries.size() == 2);
    const auto first = probe_resource(session.stream(), entries[0].body);
    REQUIRE(first.has_value());
    if (!first.has_value()) {
        return;
    }
    CHECK(first.value().kind == resource_kind::audio);
    CHECK(first.value().extension == ".wav");
    const auto second = probe_resource(session.stream(), entries[1].body);
    REQUIRE(second.has_value());
    if (!second.has_value()) {
        return;
    }
    CHECK(second.value().kind == resource_kind::font);
    CHECK(second.value().extension == ".otf");
}

TEST_CASE("godot resource converter: malformed embedded FontFile keeps its physical view",
          "[godot][resource-converter]")
{
    SECTION("unrecognised font signature")
    {
        const std::vector<std::uint8_t> invalid = {'n', 'o', 't', ' ', 'a', ' ', 'f', 'o', 'n', 't'};
        auto spec = document(4, 6);
        spec.main_type = "FontFile";
        spec.records.push_back(record("res://font", "FontFile", {property("data/0", vbytes(true, invalid))}));
        const auto stored = build_resource(spec);
        std::istringstream stream(as_text(stored));
        CHECK(!(probe_resource(stream, whole(stored)).has_value()));
    }
    SECTION("wrong property type")
    {
        auto spec = document(4, 6);
        spec.main_type = "FontFile";
        spec.records.push_back(record("res://font", "FontFile", {property("data/0", vint(true, 42))}));
        const auto stored = build_resource(spec);
        std::istringstream stream(as_text(stored));
        CHECK(!(probe_resource(stream, whole(stored)).has_value()));
    }
}

TEST_CASE("godot resource converter: probes, preserves and converts the physical shapes", "[godot][resource-converter]")
{
    SECTION("GDST preserved PNG is byte-exact")
    {
        const godot_bytes body = png_bytes(32);
        const godot_bytes image = gdst_png_texture({body});
        std::istringstream stream(as_text(image));
        const auto descriptor = probe_resource(stream, whole(image));
        REQUIRE(descriptor.has_value());
        if (!descriptor.has_value()) {
            return;
        }
        CHECK(descriptor.value_or(resource_descriptor{}).kind == resource_kind::texture);
        CHECK(descriptor.value_or(resource_descriptor{}).extension == ".png");

        std::istringstream writer(as_text(image));
        CHECK(write_collect(writer, whole(image), descriptor.value_or(resource_descriptor{})) == as_byte_vector(body));
    }
    SECTION("GST2 preserved PNG is byte-exact")
    {
        const godot_bytes body = png_bytes(24);
        const godot_bytes image = gst2_png_texture({body});
        std::istringstream stream(as_text(image));
        const auto descriptor = probe_resource(stream, whole(image));
        REQUIRE(descriptor.has_value());
        if (!descriptor.has_value()) {
            return;
        }
        CHECK(descriptor.value_or(resource_descriptor{}).extension == ".png");

        std::istringstream writer(as_text(image));
        CHECK(write_collect(writer, whole(image), descriptor.value_or(resource_descriptor{})) == as_byte_vector(body));
    }
    SECTION("GDST preserved WebP drops the wrapper and keeps the true extension")
    {
        const godot_bytes riff = webp_riff_payload();
        const godot_bytes image = gdst_webp_texture(riff);
        std::istringstream stream(as_text(image));
        const auto descriptor = probe_resource(stream, whole(image));
        REQUIRE(descriptor.has_value());
        if (!descriptor.has_value()) {
            return;
        }
        CHECK(descriptor.value_or(resource_descriptor{}).kind == resource_kind::texture);
        CHECK(descriptor.value_or(resource_descriptor{}).extension == ".webp");

        std::istringstream writer(as_text(image));
        CHECK(write_collect(writer, whole(image), descriptor.value_or(resource_descriptor{})) == as_byte_vector(riff));
    }
    SECTION("GST2 preserved WebP keeps the true extension and the plain RIFF bytes")
    {
        const godot_bytes riff = webp_riff_payload();
        const godot_bytes image = gst2_webp_texture(riff);
        std::istringstream stream(as_text(image));
        const auto descriptor = probe_resource(stream, whole(image));
        REQUIRE(descriptor.has_value());
        if (!descriptor.has_value()) {
            return;
        }
        CHECK(descriptor.value_or(resource_descriptor{}).kind == resource_kind::texture);
        CHECK(descriptor.value_or(resource_descriptor{}).extension == ".webp");

        // Godot 4 stores the standard RIFF/WebP container with no wrapper, so the converted view
        // is byte-exact.
        std::istringstream writer(as_text(image));
        CHECK(write_collect(writer, whole(image), descriptor.value_or(resource_descriptor{})) == as_byte_vector(riff));
    }
    SECTION("a raw texture converts to a PNG only when written")
    {
        const godot_bytes image = gdst_raw_texture();
        std::istringstream stream(as_text(image));
        const auto descriptor = probe_resource(stream, whole(image));
        REQUIRE(descriptor.has_value());
        if (!descriptor.has_value()) {
            return;
        }
        CHECK(descriptor.value_or(resource_descriptor{}).kind == resource_kind::texture);
        CHECK(descriptor.value_or(resource_descriptor{}).extension == ".png");

        std::istringstream writer(as_text(image));
        const std::vector<std::byte> converted =
            write_collect(writer, whole(image), descriptor.value_or(resource_descriptor{}));
        // The raw probe does not decode pixels, but the written view is a real PNG, so it is decoded
        // independently and checked for the exact dimensions and RGBA bytes of the source texel.
        const decoded_png decoded = decode_png(converted);
        CHECK(decoded.width == 1);
        CHECK(decoded.height == 1);
        CHECK(decoded.pixels == rgba({1, 2, 3, 4}));
    }
    SECTION("an RSRC raw image resource probes without decoding and writes a PNG")
    {
        const std::vector<std::uint8_t> pixels{9, 8, 7, 6};
        const godot_resource_bytes image = modern_image_resource(4, 6, 1, 1, pixels);
        std::istringstream stream(as_text(image));
        const auto descriptor = probe_resource(stream, whole(image));
        REQUIRE(descriptor.has_value());
        if (!descriptor.has_value()) {
            return;
        }
        CHECK(descriptor.value_or(resource_descriptor{}).kind == resource_kind::image);
        CHECK(descriptor.value_or(resource_descriptor{}).extension == ".png");

        std::istringstream writer(as_text(image));
        const decoded_png decoded =
            decode_png(write_collect(writer, whole(image), descriptor.value_or(resource_descriptor{})));
        CHECK(decoded.width == 1);
        CHECK(decoded.height == 1);
        CHECK(decoded.pixels == rgba({9, 8, 7, 6}));
    }
    SECTION("a preserved Godot 2 embedded PNG image is byte-exact")
    {
        const godot_bytes inner = png_bytes(20);
        godot_bytes envelope = {'P', 'N', 'G', ' '};
        envelope.insert(envelope.end(), inner.begin(), inner.end());
        const godot_resource_bytes image = g2_encoded_image_resource(2, envelope);
        std::istringstream stream(as_text(image));
        const auto descriptor = probe_resource(stream, whole(image));
        REQUIRE(descriptor.has_value());
        if (!descriptor.has_value()) {
            return;
        }
        CHECK(descriptor.value_or(resource_descriptor{}).kind == resource_kind::image);
        CHECK(descriptor.value_or(resource_descriptor{}).extension == ".png");

        std::istringstream writer(as_text(image));
        CHECK(write_collect(writer, whole(image), descriptor.value_or(resource_descriptor{})) == as_byte_vector(inner));
    }
}

TEST_CASE("godot resource converter: unrecognised and malformed candidates decline the view",
          "[godot][resource-converter]")
{
    SECTION("an unrelated byte string has no convertible view")
    {
        const godot_bytes plain{'h', 'e', 'l', 'l', 'o'};
        std::istringstream stream(as_text(plain));
        CHECK(!(probe_resource(stream, whole(plain)).has_value()));
    }
    SECTION("a valid non-image resource has no convertible view")
    {
        const godot_resource_bytes resource = non_image_resource();
        std::istringstream stream(as_text(resource));
        CHECK(!(probe_resource(stream, whole(resource)).has_value()));
    }
    SECTION("a truncated texture envelope declines without a blanket source error")
    {
        godot_bytes image = gdst_png_texture({png_bytes(16)});
        image.resize(10); // fewer than the fixed 20-byte header
        std::istringstream stream(as_text(image));
        CHECK_NOTHROW(probe_resource(stream, whole(image)));
        std::istringstream second(as_text(image));
        CHECK(!(probe_resource(second, whole(image)).has_value()));
    }
}

TEST_CASE("godot resource converter: classifies source faults and preserves sink exceptions",
          "[godot][resource-converter]")
{
    SECTION("a source seek/read fault is a resource_source_error")
    {
        unreadable_stream stream(64);
        CHECK_THROWS_AS(probe_resource(stream, byte_range{0, 64}), resource_source_error);
    }
    SECTION("a bad-format candidate is not a resource_source_error")
    {
        godot_bytes image = gdst_png_texture({png_bytes(16)});
        image.resize(10);
        std::istringstream stream(as_text(image));
        bool source_error = false;
        bool unexpected_error = false;
        bool declined = false;
        try {
            declined = !probe_resource(stream, whole(image)).has_value();
        } catch (const resource_source_error &) {
            source_error = true;
        } catch (...) {
            unexpected_error = true;
        }
        CHECK(!(source_error));
        CHECK(!(unexpected_error));
        CHECK(declined);
    }
    SECTION("a sink exception propagates unchanged from write_resource")
    {
        struct sink_failure final : std::runtime_error
        {
            sink_failure() : std::runtime_error("sink failure")
            {
            }
        };
        const godot_bytes image = gdst_png_texture({png_bytes(16)});
        std::istringstream stream(as_text(image));
        const auto descriptor = probe_resource(stream, whole(image));
        REQUIRE(descriptor.has_value());
        if (!descriptor.has_value()) {
            return;
        }

        std::istringstream writer(as_text(image));
        CHECK_THROWS_AS(write_resource(writer, whole(image), descriptor.value_or(resource_descriptor{}),
                                       [](const std::span<const std::byte>) { throw sink_failure{}; }),
                        sink_failure);
    }
}

TEST_CASE("godot resource converter: unsupported texture families decline the view", "[godot][resource-converter]")
{
    SECTION("a GST2 Basis Universal container declines without faulting")
    {
        gst2_fixture fixture;
        fixture.display_width = 64;
        fixture.display_height = 64;
        fixture.image_data_format = test::support::gst2_data_basis;
        fixture.image_width = 8;
        fixture.image_height = 8;
        fixture.image_format = test::support::image_format_rgba8;
        fixture.raw_data = godot_bytes(64);
        const godot_bytes image = build_gst2(fixture);
        std::istringstream stream(as_text(image));
        CHECK_NOTHROW(probe_resource(stream, whole(image)));
        std::istringstream second(as_text(image));
        CHECK(!(probe_resource(second, whole(image)).has_value()));
    }
    SECTION("a GDST raw texture in an unmodelled pixel format declines without faulting")
    {
        gdst_fixture fixture;
        fixture.width = 1;
        fixture.height = 1;
        fixture.data_format = test::support::image_format_astc_4x4;
        fixture.raw_data = godot_bytes(16);
        const godot_bytes image = build_gdst(fixture);
        std::istringstream stream(as_text(image));
        CHECK_NOTHROW(probe_resource(stream, whole(image)));
        std::istringstream second(as_text(image));
        CHECK(!(probe_resource(second, whole(image)).has_value()));
    }
}

TEST_CASE("godot resource converter: window bounds are validated before any large read", "[godot][resource-converter]")
{
    // A 4-byte "RSRC" magic is present so the leading sniff succeeds; the dispatcher, not the
    // stream, is what must reject an out-of-bounds or over-policy window.
    const std::string stored = std::string("RSRC") + std::string(4, '\0');

    SECTION("a window reaching past the physical stream is a source fault with no window read")
    {
        reporting_stream stream(stored, stored.size());
        std::vector<std::byte> output;
        const auto sink = [&output](const std::span<const std::byte> chunk) {
            output.insert(output.end(), chunk.begin(), chunk.end());
        };
        // A 64 MiB window over an 8-byte stream: the physical range must be rejected before the
        // large buffer is allocated or any of it read.
        bool source_error = false;
        bool unexpected_error = false;
        try {
            write_resource(stream, byte_range{0, 64ULL * 1024 * 1024},
                           resource_descriptor{resource_kind::image, ".png"}, sink);
        } catch (const resource_source_error &) {
            source_error = true;
        } catch (...) {
            unexpected_error = true;
        }
        CHECK(source_error);
        CHECK(!(unexpected_error));
        // Only the 4-byte magic sniff was ever fetched; the huge window was never read.
        CHECK(stream.largest_request() <= 4);
        CHECK(output.empty());
    }
}

TEST_CASE("godot resource converter: a malformed compressed payload is not a source fault",
          "[godot][resource-converter]")
{
    const std::vector<std::uint8_t> pixels = zero_bytes(std::size_t{64} * 64 * 4);
    resource_document_spec logical_spec = document(4, 6);
    logical_spec.include_magic = false;
    logical_spec.records.push_back(
        record("local://0", "Image", {property("data", image_data(64, 64, "RGBA8", pixels))}));
    logical_spec.records.push_back(
        record("res://tex", "ImageTexture",
               {property("image", encode(true, [](variant_encoder &encoder) { encoder.object_internal(0); }))}));
    logical_spec.main_type = "ImageTexture";
    const godot_resource_bytes logical = build_resource(logical_spec);

    std::vector<std::byte> corrupt = build_rscc(as_byte_vector(logical), compression_kind::deflate, 4096);
    REQUIRE(corrupt.size() > 8);
    corrupt.at(corrupt.size() - 5) ^= std::byte{0xFF}; // corrupt the compressed body, before the magic

    std::vector<std::byte> output;
    bool threw = false;
    std::istringstream writer(as_text(corrupt));
    try {
        write_resource(writer, whole(corrupt), resource_descriptor{resource_kind::image, ".png"},
                       [&output](const std::span<const std::byte> chunk) {
                           output.insert(output.end(), chunk.begin(), chunk.end());
                       });
    } catch (const resource_source_error &) {
        FAIL("a malformed payload must not be reported as a source error");
    } catch (...) {
        threw = true;
    }
    CHECK(threw);
    CHECK(output.empty());
}

TEST_CASE("godot resource converter: a source stream fault is reported as a source error",
          "[godot][resource-converter]")
{
    SECTION("a stream that cannot report its current position is a source fault")
    {
        // The texture prober first discovers the stream extent; a stream that cannot even answer a
        // current-position seek is a genuine source fault, never a declined but healthy candidate.
        fault_stream stream(std::string("GDST") + std::string(28, '\0'), 32);
        stream.buffer().fail_current_seek = true;
        CHECK_THROWS_AS(probe_resource(stream, byte_range{0, 32}), resource_source_error);
    }
    SECTION("a stream whose end precedes its current position is a source fault")
    {
        // A source that shrank below the position it already reported: the reported end is fewer
        // bytes than the current position, so the physical window can no longer be trusted.
        fault_stream stream(std::string(4, '\0'), 4, 10);
        std::vector<std::byte> output;
        const auto sink = [&output](const std::span<const std::byte> chunk) {
            output.insert(output.end(), chunk.begin(), chunk.end());
        };
        CHECK_THROWS_AS(
            write_resource(stream, byte_range{0, 2}, resource_descriptor{resource_kind::image, ".png"}, sink),
            resource_source_error);
        CHECK(output.empty());
    }
    SECTION("a seek that raises the stream exception is a source fault")
    {
        fault_stream stream(std::string("GDST") + std::string(28, '\0'), 32);
        stream.buffer().fail_end_seek = true;
        stream.exceptions(static_cast<std::ios_base::iostate>(static_cast<unsigned>(std::ios::failbit) |
                                                              static_cast<unsigned>(std::ios::badbit)));
        CHECK_THROWS_AS(probe_resource(stream, byte_range{0, 32}), resource_source_error);
    }
    SECTION("a seek past the physical stream is a source fault")
    {
        counting_stream stream(std::string("RSRC"));
        CHECK_THROWS_AS(probe_resource(stream, byte_range{100, 4}), resource_source_error);
    }
    SECTION("a read that raises the stream exception is a source fault")
    {
        unreadable_stream stream(64);
        stream.exceptions(static_cast<std::ios_base::iostate>(static_cast<unsigned>(std::ios::failbit) |
                                                              static_cast<unsigned>(std::ios::badbit)));
        CHECK_THROWS_AS(probe_resource(stream, byte_range{0, 64}), resource_source_error);
    }
}

TEST_CASE("godot resource converter: a window that wraps the address space is a source fault",
          "[godot][resource-converter]")
{
    // A two-byte window based at the very top of the address space wraps ``offset + size``. The
    // window can never fit any stream, so it is a source fault and no byte is written.
    std::istringstream stream(std::string("RSRC"));
    std::vector<std::byte> output;
    const auto sink = [&output](const std::span<const std::byte> chunk) {
        output.insert(output.end(), chunk.begin(), chunk.end());
    };
    constexpr std::uint64_t base = (std::numeric_limits<std::uint64_t>::max)();
    CHECK_THROWS_AS(
        write_resource(stream, byte_range{base, 2}, resource_descriptor{resource_kind::image, ".png"}, sink),
        resource_source_error);
    CHECK(output.empty());
}

TEST_CASE("godot resource converter: a source that runs dry mid-read is a source fault", "[godot][resource-converter]")
{
    // The stream holds only the fixed GDST header and the mip count, yet reports the full extent, so
    // the first per-level length read falls off the delivered bytes.
    const godot_bytes full = gdst_png_texture({png_bytes(16)});
    const std::string prefix = as_text(full).substr(0, 24);

    SECTION("the underlying stream raises on the short read")
    {
        fault_stream stream(prefix, full.size());
        stream.exceptions(static_cast<std::ios_base::iostate>(static_cast<unsigned>(std::ios::failbit) |
                                                              static_cast<unsigned>(std::ios::badbit)));
        CHECK_THROWS_AS(probe_resource(stream, whole(full)), resource_source_error);
    }
    SECTION("the underlying stream delivers nothing and the reader traps it")
    {
        fault_stream stream(prefix, full.size());
        CHECK_THROWS_AS(probe_resource(stream, whole(full)), resource_source_error);
    }
}

TEST_CASE("godot resource converter: a window that reaches past the stream is a source fault",
          "[godot][resource-converter]")
{
    const std::string body = std::string("GDST") + std::string(96, '\0');

    SECTION("probing a texture whose declared window exceeds the stream")
    {
        std::istringstream stream(body);
        CHECK_THROWS_AS(probe_resource(stream, byte_range{0, 200}), resource_source_error);
    }
    SECTION("writing a texture whose declared window exceeds the stream")
    {
        std::istringstream stream(body);
        std::vector<std::byte> output;
        const auto sink = [&output](const std::span<const std::byte> chunk) {
            output.insert(output.end(), chunk.begin(), chunk.end());
        };
        CHECK_THROWS_AS(
            write_resource(stream, byte_range{0, 200}, resource_descriptor{resource_kind::texture, ".png"}, sink),
            resource_source_error);
        CHECK(output.empty());
    }
}

TEST_CASE("godot resource converter: malformed binary containers decline the view", "[godot][resource-converter]")
{
    SECTION("a malformed RSCC envelope declines")
    {
        const std::vector<std::uint8_t> stored = {'R', 'S', 'C', 'C', 0, 0, 0, 0};
        std::istringstream stream(as_text(stored));
        CHECK(!(probe_resource(stream, whole(stored)).has_value()));
    }
    SECTION("a valid RSCC image remains convertible regardless of its logical size")
    {
        const std::vector<std::uint8_t> pixels = zero_bytes(std::size_t{64} * 64 * 4);
        resource_document_spec logical_spec = document(4, 6);
        logical_spec.include_magic = false;
        logical_spec.records.push_back(
            record("local://0", "Image", {property("data", image_data(64, 64, "RGBA8", pixels))}));
        logical_spec.records.push_back(
            record("res://tex", "ImageTexture",
                   {property("image", encode(true, [](variant_encoder &encoder) { encoder.object_internal(0); }))}));
        logical_spec.main_type = "ImageTexture";
        const godot_resource_bytes logical = build_resource(logical_spec);
        const std::vector<std::byte> stored = build_rscc(as_byte_vector(logical), compression_kind::deflate, 4096);
        std::istringstream stream(as_text(stored));
        const auto descriptor = probe_resource(stream, whole(stored));
        REQUIRE(descriptor.has_value());
        if (!descriptor.has_value()) {
            return;
        }
        CHECK(descriptor.value().kind == resource_kind::image);
        CHECK(descriptor.value().extension == ".png");
    }
    SECTION("a malformed RSRC payload declines")
    {
        const std::vector<std::uint8_t> stored = {'R', 'S', 'R', 'C', 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
        std::istringstream stream(as_text(stored));
        CHECK(!(probe_resource(stream, whole(stored)).has_value()));
    }
}

TEST_CASE("godot resource converter: a binary audio resource probes and writes as WAV", "[godot][resource-converter]")
{
    const std::vector<std::uint8_t> pcm = {0x00, 0x11, 0x22, 0x33};
    const godot_resource_bytes audio = audio_wave_resource(1, 8000, pcm);
    std::istringstream stream(as_text(audio));
    const auto descriptor = probe_resource(stream, whole(audio));
    REQUIRE(descriptor.has_value());
    if (!descriptor.has_value()) {
        return;
    }
    CHECK(descriptor.value_or(resource_descriptor{}).kind == resource_kind::audio);
    CHECK(descriptor.value_or(resource_descriptor{}).extension == ".wav");

    // The written view is a real RIFF/WAVE container, not the stored binary resource.
    std::istringstream writer(as_text(audio));
    const std::string text = as_text(write_collect(writer, whole(audio), descriptor.value_or(resource_descriptor{})));
    REQUIRE(text.size() >= 12);
    CHECK(text.substr(0, 4) == "RIFF");
    CHECK(text.substr(8, 4) == "WAVE");
}

TEST_CASE("godot resource converter: a malformed audio descriptor and a bodiless image decline",
          "[godot][resource-converter]")
{
    SECTION("an AudioStreamWAV with an invalid sample rate declines without faulting")
    {
        const std::vector<std::uint8_t> pcm = {0x00, 0x11};
        const godot_resource_bytes audio = audio_wave_resource(1, -1, pcm);
        std::istringstream stream(as_text(audio));
        CHECK(!(probe_resource(stream, whole(audio)).has_value()));
    }
    SECTION("an ImageTexture with no image property declines without faulting")
    {
        resource_document_spec spec = document(4, 6);
        spec.main_type = "ImageTexture";
        spec.records.push_back(record("res://tex", "ImageTexture", {}));
        const godot_resource_bytes resource = build_resource(spec);
        std::istringstream stream(as_text(resource));
        CHECK(!(probe_resource(stream, whole(resource)).has_value()));
    }
}

TEST_CASE("godot resource converter: a preserved level ending exactly at the window end reads fully",
          "[godot][resource-converter]")
{
    // The 8-byte PNG signature: the smallest encoded level the texture prober preserves verbatim.
    const godot_bytes png_signature = {0x89, 'P', 'N', 'G', 0x0D, 0x0A, 0x1A, 0x0A};
    // The single level is exactly the 8-byte PNG signature, so the level-signature read consumes the
    // last bytes of the window with nothing left over; the converted view is that exact body.
    const godot_bytes image = gdst_png_texture({png_signature});
    std::istringstream stream(as_text(image));
    const auto descriptor = probe_resource(stream, whole(image));
    REQUIRE(descriptor.has_value());
    if (!descriptor.has_value()) {
        return;
    }
    CHECK(descriptor.value_or(resource_descriptor{}).kind == resource_kind::texture);
    CHECK(descriptor.value_or(resource_descriptor{}).extension == ".png");

    std::istringstream writer(as_text(image));
    CHECK(write_collect(writer, whole(image), descriptor.value_or(resource_descriptor{})) ==
          as_byte_vector(png_signature));
}

TEST_CASE("godot resource converter: a preserved level larger than one chunk streams byte-exact",
          "[godot][resource-converter]")
{
    // A 70 KiB preserved PNG body exceeds the fixed 64 KiB streaming chunk, so the copy loop takes
    // the full-chunk path at least once; the whole body must still round-trip byte for byte.
    const godot_bytes body = png_bytes(std::size_t{70} * 1024);
    const godot_bytes image = gdst_png_texture({body});
    std::istringstream stream(as_text(image));
    const auto descriptor = probe_resource(stream, whole(image));
    REQUIRE(descriptor.has_value());
    if (!descriptor.has_value()) {
        return;
    }

    std::istringstream writer(as_text(image));
    CHECK(write_collect(writer, whole(image), descriptor.value_or(resource_descriptor{})) == as_byte_vector(body));
}

TEST_CASE("godot resource converter: extraction rejects a changed or mismatched binary descriptor",
          "[godot][resource-converter][descriptor]")
{
    const std::vector<std::uint8_t> font = {0x00, 0x01, 0x00, 0x00, 0, 0, 0, 0, 0, 0, 0, 0};
    auto font_spec = document(4, 6);
    font_spec.main_type = "FontFile";
    font_spec.records.push_back(
        record("res://font", "FontFile", {property("unused", vint(true, 42)), property("data", vbytes(true, font))}));
    const auto font_source = build_resource(font_spec);
    std::istringstream font_writer(as_text(font_source));
    const auto font_descriptor = probe_resource(font_writer, whole(font_source));
    REQUIRE(font_descriptor.has_value());
    if (!font_descriptor.has_value()) {
        return;
    }
    CHECK(font_descriptor.value().kind == resource_kind::font);
    CHECK(write_collect(font_writer, whole(font_source), *font_descriptor) == as_byte_vector(font));
    CHECK_THROWS_AS(write_collect(font_writer, whole(font_source), resource_descriptor{resource_kind::font, ".otf"}),
                    unpackrat::godot::font_error);
    CHECK_THROWS_AS(write_collect(font_writer, whole(font_source), resource_descriptor{resource_kind::audio, ".wav"}),
                    unpackrat::codecs::audio_error);
    CHECK_THROWS_AS(write_collect(font_writer, whole(font_source), resource_descriptor{resource_kind::image, ".png"}),
                    unpackrat::codecs::image_error);

    const std::vector<std::uint8_t> audio_samples = {0x00, 0x11};
    const godot_resource_bytes audio_source = audio_wave_resource(1, 8000, audio_samples);
    std::istringstream audio_writer(as_text(audio_source));
    CHECK_THROWS_AS(write_collect(audio_writer, whole(audio_source), resource_descriptor{resource_kind::audio, ".ogg"}),
                    unpackrat::codecs::audio_error);
    CHECK_THROWS_AS(write_collect(audio_writer, whole(audio_source), resource_descriptor{resource_kind::font, ".ttf"}),
                    unpackrat::godot::font_error);

    const auto image_source = modern_image_resource(4, 6, 2, 2, zero_bytes(16));
    std::istringstream image_writer(as_text(image_source));
    CHECK_THROWS_AS(
        write_collect(image_writer, whole(image_source), resource_descriptor{resource_kind::image, ".webp"}),
        unpackrat::codecs::image_error);

    std::istringstream raw("raw bytes");
    CHECK_THROWS_AS(write_collect(raw, byte_range{0, 9}, resource_descriptor{resource_kind::image, ".png"}),
                    unpackrat::godot::resource_error);
}

TEST_CASE("godot resource converter: binary source position and seek failures stay source errors",
          "[godot][resource-converter][binary-source]")
{
    const auto sink = [](std::span<const std::byte>) { FAIL("source failure must precede output"); };
    SECTION("unreported current position")
    {
        fault_stream stream("RSRC", 4);
        stream.buffer().fail_current_seek = true;
        CHECK_THROWS_AS(
            write_resource(stream, byte_range{0, 4}, resource_descriptor{resource_kind::image, ".png"}, sink),
            resource_source_error);
    }
    SECTION("throwing end seek")
    {
        fault_stream stream("RSRC", 4);
        stream.buffer().fail_end_seek = true;
        stream.exceptions(static_cast<std::ios_base::iostate>(static_cast<unsigned>(std::ios::failbit) |
                                                              static_cast<unsigned>(std::ios::badbit)));
        CHECK_THROWS_AS(
            write_resource(stream, byte_range{0, 4}, resource_descriptor{resource_kind::image, ".png"}, sink),
            resource_source_error);
    }
}

namespace
{
    class seek_count_buffer final : public std::stringbuf
    {
      public:
        explicit seek_count_buffer(const std::string &data) : std::stringbuf(data, std::ios::in)
        {
        }
        std::size_t seeks = 0;
        std::size_t end_scans = 0;

      protected:
        pos_type seekoff(const off_type offset, const std::ios_base::seekdir direction,
                         const std::ios_base::openmode mode) override
        {
            ++seeks;
            if (direction == std::ios_base::end) {
                ++end_scans;
            }
            return std::stringbuf::seekoff(offset, direction, mode);
        }
        pos_type seekpos(const pos_type position, const std::ios_base::openmode mode) override
        {
            ++seeks;
            return std::stringbuf::seekpos(position, mode);
        }
    };
    class seek_count_stream final : public std::istream
    {
      public:
        explicit seek_count_stream(const std::string &data) : std::istream(nullptr), buffer_(data)
        {
            rdbuf(&buffer_);
            clear();
        }
        const seek_count_buffer &counts() const noexcept
        {
            return buffer_;
        }

      private:
        seek_count_buffer buffer_;
    };
} // namespace

TEST_CASE("godot resource converter: sequential metadata reads avoid redundant seeks",
          "[godot][resource-converter][perf2-converter]")
{
    const auto resource = modern_image_resource(4, 6, 2, 2, zero_bytes(16));
    seek_count_stream stream(as_text(resource));
    const auto descriptor = probe_resource(stream, whole(resource));
    REQUIRE(descriptor.has_value());
    if (!descriptor.has_value()) {
        return;
    }
    CHECK(descriptor->kind == resource_kind::image);
    CHECK(descriptor->extension == ".png");
    CHECK(stream.counts().end_scans == 1);
    CHECK(stream.counts().seeks <= 12);
}

TEST_CASE("godot resource converter: unsupported header skips dense resource tables",
          "[godot][resource-converter][perf2-converter]")
{
    const bool compressed = GENERATE(false, true);
    auto spec = document(4, 6);
    spec.include_magic = !compressed;
    spec.main_type = "PackedScene";
    spec.records.push_back(record("res://scene", "PackedScene", {}));
    for (std::size_t index = 0; index < 4096; ++index) {
        spec.string_table.push_back("unused-field-" + std::to_string(index));
    }
    const auto logical = build_resource(spec);
    const auto stored =
        compressed ? build_rscc(as_byte_vector(logical), compression_kind::zstd, 128) : as_byte_vector(logical);
    counting_stream stream(as_text(stored));
    CHECK(!(probe_resource(stream, whole(stored)).has_value()));
    const std::size_t table_bytes = compressed ? (logical.size() / 128 + 1) * 4 : 0;
    CHECK(stream.bytes_read() < table_bytes + 1024);
}

TEST_CASE("godot resource converter: one size scan and callback seeks preserve font extraction",
          "[godot][resource-converter][perf2-converter]")
{
    std::vector<std::uint8_t> font(std::size_t{130} * 1024, 0x39);
    font[0] = 'O';
    font[1] = 'T';
    font[2] = 'T';
    font[3] = 'O';
    auto spec = document(4, 6);
    spec.main_type = "FontFile";
    spec.records.push_back(record("res://font", "FontFile", {property("data", vbytes(true, font))}));
    const auto resource = build_resource(spec);
    seek_count_stream stream(as_text(resource));
    std::vector<std::byte> output;
    write_resource(stream, whole(resource), resource_descriptor{resource_kind::font, ".otf"},
                   [&](const std::span<const std::byte> chunk) {
                       output.insert(output.end(), chunk.begin(), chunk.end());
                       stream.clear();
                       stream.seekg(0);
                   });
    CHECK(output == as_byte_vector(font));
    CHECK(stream.counts().end_scans == 1);
}
