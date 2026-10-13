#include "godot_texture_fixtures.h"

#include <cstring>
#include <ios>
#include <utility>

namespace test::support
{
    namespace
    {
        void append_u16(godot_bytes &output, const std::uint16_t value)
        {
            output.push_back(static_cast<std::uint8_t>(value));
            output.push_back(static_cast<std::uint8_t>(value >> 8));
        }

        void append_u32(godot_bytes &output, const std::uint32_t value)
        {
            output.push_back(static_cast<std::uint8_t>(value));
            output.push_back(static_cast<std::uint8_t>(value >> 8));
            output.push_back(static_cast<std::uint8_t>(value >> 16));
            output.push_back(static_cast<std::uint8_t>(value >> 24));
        }

        void append_bytes(godot_bytes &output, const godot_bytes &bytes)
        {
            output.insert(output.end(), bytes.begin(), bytes.end());
        }

        // Writes exactly four magic bytes; a shorter string is NUL-padded, a longer one truncated.
        void append_magic(godot_bytes &output, const std::string &magic)
        {
            for (std::size_t index = 0; index < 4; ++index) {
                const auto value = index < magic.size() ? static_cast<std::uint8_t>(magic[index]) : std::uint8_t{0};
                output.push_back(value);
            }
        }

        void apply_truncation(godot_bytes &output, const std::optional<std::size_t> &limit)
        {
            if (limit.has_value()) {
                output.resize(*limit);
            }
        }
    } // namespace

    godot_bytes build_gdst(const gdst_fixture &fixture)
    {
        godot_bytes output;
        append_magic(output, fixture.magic);
        append_u16(output, fixture.width);
        append_u16(output, fixture.custom_width);
        append_u16(output, fixture.height);
        append_u16(output, fixture.custom_height);
        append_u32(output, fixture.flags);
        append_u32(output, fixture.data_format);

        if (fixture.raw_data.has_value()) {
            append_bytes(output, *fixture.raw_data);
        } else {
            const auto count = fixture.mipmap_count.value_or(static_cast<std::uint32_t>(fixture.mips.size()));
            append_u32(output, count);
            for (const godot_bytes &mip : fixture.mips) {
                append_u32(output, static_cast<std::uint32_t>(mip.size()));
                append_bytes(output, mip);
            }
        }

        apply_truncation(output, fixture.truncate_to);
        return output;
    }

    godot_bytes build_gst2(const gst2_fixture &fixture)
    {
        godot_bytes output;
        append_magic(output, fixture.magic);
        append_u32(output, fixture.version);
        if (fixture.scale_tag.has_value()) {
            append_u16(output, fixture.scale_tag.value());
        }
        append_u32(output, fixture.display_width);
        append_u32(output, fixture.display_height);
        append_u32(output, fixture.data_format);
        append_u32(output, fixture.mipmap_limit);
        for (const std::uint32_t reserved : fixture.reserved) {
            append_u32(output, reserved);
        }

        // Image metadata block (4.5 CompressedTexture2D::load_image_from_file).
        append_u32(output, fixture.image_data_format);
        append_u16(output, fixture.image_width);
        append_u16(output, fixture.image_height);
        const auto declared_mipmaps = fixture.image_mipmaps.value_or(
            fixture.raw_data.has_value() || fixture.mips.empty() ? 0U
                                                                 : static_cast<std::uint32_t>(fixture.mips.size() - 1));
        append_u32(output, declared_mipmaps);
        append_u32(output, fixture.image_format);

        if (fixture.raw_data.has_value()) {
            append_bytes(output, *fixture.raw_data);
        } else {
            for (const godot_bytes &mip : fixture.mips) {
                append_u32(output, static_cast<std::uint32_t>(mip.size()));
                append_bytes(output, mip);
            }
        }

        apply_truncation(output, fixture.truncate_to);
        return output;
    }

    godot_bytes png_bytes(const std::size_t total)
    {
        static const std::uint8_t signature[8] = {0x89, 'P', 'N', 'G', 0x0D, 0x0A, 0x1A, 0x0A};
        const std::size_t size = total < sizeof(signature) ? sizeof(signature) : total;
        godot_bytes output(size, std::uint8_t{0});
        std::memcpy(output.data(), signature, sizeof(signature));
        return output;
    }

    godot_bytes webp_riff_payload()
    {
        godot_bytes output = {'R', 'I', 'F', 'F'};
        append_u32(output, 12); // RIFF size: "WEBP" fourcc + the 8-byte "VP8L" chunk header
        append_bytes(output, {'W', 'E', 'B', 'P'});
        append_bytes(output, {'V', 'P', '8', 'L'});
        append_u32(output, 0x2F);
        return output;
    }

    std::uint64_t godot_level_bytes(const std::uint32_t format, const std::uint32_t width, const std::uint32_t height)
    {
        std::uint32_t block_width = 1;
        std::uint32_t block_height = 1;
        std::uint32_t bytes_per_block = 0;

        switch (format) {
        case image_format_l8:
        case image_format_r8:
            bytes_per_block = 1;
            break;
        case image_format_la8:
        case image_format_rg8:
            bytes_per_block = 2;
            break;
        case image_format_rgb8:
            bytes_per_block = 3;
            break;
        case image_format_rgba8:
            bytes_per_block = 4;
            break;
        case image_format_dxt1:
        case image_format_rgtc_r:
            block_width = 4;
            block_height = 4;
            bytes_per_block = 8;
            break;
        case image_format_dxt3:
        case image_format_dxt5:
        case image_format_rgtc_rg:
        case image_format_bptc_rgba:
            block_width = 4;
            block_height = 4;
            bytes_per_block = 16;
            break;
        default:
            return 0;
        }

        if (block_width == 1) {
            return static_cast<std::uint64_t>(width) * height * bytes_per_block;
        }

        const std::uint64_t blocks_x = (static_cast<std::uint64_t>(width) + block_width - 1) / block_width;
        const std::uint64_t blocks_y = (static_cast<std::uint64_t>(height) + block_height - 1) / block_height;
        return blocks_x * blocks_y * bytes_per_block;
    }

    counting_streambuf::counting_streambuf(std::string data) : data_(std::move(data))
    {
    }

    std::uint64_t counting_streambuf::bytes_read() const noexcept
    {
        return bytes_read_;
    }

    counting_streambuf::int_type counting_streambuf::underflow()
    {
        if (position_ >= data_.size()) {
            return traits_type::eof();
        }
        return traits_type::to_int_type(data_[position_]);
    }

    counting_streambuf::int_type counting_streambuf::uflow()
    {
        if (position_ >= data_.size()) {
            return traits_type::eof();
        }
        const char_type value = data_[position_++];
        ++bytes_read_;
        return traits_type::to_int_type(value);
    }

    std::streamsize counting_streambuf::xsgetn(char_type *target, const std::streamsize count)
    {
        const std::streamsize available = static_cast<std::streamsize>(data_.size() - position_);
        const std::streamsize delivered = count < available ? count : available;
        if (delivered > 0) {
            std::memcpy(target, data_.data() + position_, static_cast<std::size_t>(delivered));
            position_ += static_cast<std::size_t>(delivered);
            bytes_read_ += static_cast<std::uint64_t>(delivered);
        }
        return delivered;
    }

    counting_streambuf::pos_type counting_streambuf::seekoff(const off_type offset,
                                                             const std::ios_base::seekdir direction,
                                                             const std::ios_base::openmode mode)
    {
        if ((mode & std::ios_base::in) == 0) {
            return pos_type(off_type(-1));
        }

        std::int64_t base = 0;
        if (direction == std::ios_base::cur) {
            base = static_cast<std::int64_t>(position_);
        } else if (direction == std::ios_base::end) {
            base = static_cast<std::int64_t>(data_.size());
        }

        const std::int64_t target = base + static_cast<std::int64_t>(offset);
        if (target < 0 || target > static_cast<std::int64_t>(data_.size())) {
            return pos_type(off_type(-1));
        }

        position_ = static_cast<std::size_t>(target);
        return pos_type(off_type(position_));
    }

    counting_streambuf::pos_type counting_streambuf::seekpos(const pos_type position,
                                                             const std::ios_base::openmode mode)
    {
        return seekoff(off_type(position), std::ios_base::beg, mode);
    }

    counting_stream::counting_stream(std::string data) : std::istream(nullptr), buffer_(std::move(data))
    {
        rdbuf(&buffer_);
        clear();
    }

    std::uint64_t counting_stream::bytes_read() const noexcept
    {
        return buffer_.bytes_read();
    }

    unreadable_streambuf::unreadable_streambuf(const std::streamoff nominal_size) : size_(nominal_size)
    {
    }

    unreadable_streambuf::int_type unreadable_streambuf::underflow()
    {
        return traits_type::eof();
    }

    unreadable_streambuf::int_type unreadable_streambuf::uflow()
    {
        return traits_type::eof();
    }

    std::streamsize unreadable_streambuf::xsgetn(char_type *, const std::streamsize)
    {
        return 0;
    }

    unreadable_streambuf::pos_type unreadable_streambuf::seekoff(const off_type offset,
                                                                 const std::ios_base::seekdir direction,
                                                                 const std::ios_base::openmode mode)
    {
        if ((mode & std::ios_base::in) == 0) {
            return pos_type(off_type(-1));
        }
        std::streamoff base = 0;
        if (direction == std::ios_base::cur) {
            base = position_;
        } else if (direction == std::ios_base::end) {
            base = size_;
        }
        const std::streamoff target = base + offset;
        if (target < 0 || target > size_) {
            return pos_type(off_type(-1));
        }
        position_ = target;
        return pos_type(position_);
    }

    unreadable_streambuf::pos_type unreadable_streambuf::seekpos(const pos_type position,
                                                                 const std::ios_base::openmode mode)
    {
        return seekoff(off_type(position), std::ios_base::beg, mode);
    }

    unreadable_stream::unreadable_stream(const std::streamoff nominal_size)
        : std::istream(nullptr), buffer_(nominal_size)
    {
        rdbuf(&buffer_);
        clear();
    }
} // namespace test::support
