#include "godot_fixtures.h"

#include <algorithm>
#include <cstdint>
#include <iterator>
#include <string>
#include <utility>

namespace test::support
{
    namespace
    {
        // Fixed minimal PE geometry shared by the fixture wrappers and their expectations.
        constexpr std::uint32_t fixture_pe_offset = 0x80;
        constexpr std::uint16_t fixture_optional_size = 0xF0;
        constexpr std::uint64_t fixture_section_table = fixture_pe_offset + 24 + fixture_optional_size;
        constexpr std::uint64_t fixture_raw_offset = ((fixture_section_table + 40 + 0x1FF) / 0x200) * 0x200;

        void append_u32(godot_bytes &output, const std::uint32_t value)
        {
            output.push_back(static_cast<std::uint8_t>(value));
            output.push_back(static_cast<std::uint8_t>(value >> 8));
            output.push_back(static_cast<std::uint8_t>(value >> 16));
            output.push_back(static_cast<std::uint8_t>(value >> 24));
        }

        void append_u64(godot_bytes &output, const std::uint64_t value)
        {
            append_u32(output, static_cast<std::uint32_t>(value));
            append_u32(output, static_cast<std::uint32_t>(value >> 32));
        }

        void write_section_name(godot_bytes &bytes, const std::size_t offset, const std::string &name)
        {
            for (std::size_t index = 0; index < 8; ++index) {
                const auto character = index < name.size() ? static_cast<std::uint8_t>(name[index]) : std::uint8_t{0};
                bytes[offset + index] = character;
            }
        }

        // The official Godot packer pads every stored entry name up to a 4-byte boundary and
        // fills the remainder with NUL bytes, writing the padded length as the name-length field.
        std::vector<std::uint8_t> stored_name(const godot_entry_options &entry)
        {
            if (entry.raw_name.has_value()) {
                return *entry.raw_name;
            }
            std::vector<std::uint8_t> name;
            name.reserve(entry.path.size() + 3);
            std::transform(entry.path.begin(), entry.path.end(), std::back_inserter(name), [](const char character) {
                return static_cast<std::uint8_t>(static_cast<unsigned char>(character));
            });
            const std::size_t padded = (name.size() + 3) & ~static_cast<std::size_t>(3);
            name.resize(padded, std::uint8_t{0});
            return name;
        }

    } // namespace

    void patch_u16(godot_bytes &bytes, const std::size_t offset, const std::uint16_t value)
    {
        bytes.at(offset) = static_cast<std::uint8_t>(value);
        bytes.at(offset + 1) = static_cast<std::uint8_t>(value >> 8);
    }

    void patch_u32(godot_bytes &bytes, const std::size_t offset, const std::uint32_t value)
    {
        bytes.at(offset) = static_cast<std::uint8_t>(value);
        bytes.at(offset + 1) = static_cast<std::uint8_t>(value >> 8);
        bytes.at(offset + 2) = static_cast<std::uint8_t>(value >> 16);
        bytes.at(offset + 3) = static_cast<std::uint8_t>(value >> 24);
    }

    void patch_u64(godot_bytes &bytes, const std::size_t offset, const std::uint64_t value)
    {
        patch_u32(bytes, offset, static_cast<std::uint32_t>(value));
        patch_u32(bytes, offset + 4, static_cast<std::uint32_t>(value >> 32));
    }

    godot_bytes build_godot_pck(const godot_pck_options &options)
    {
        const bool has_flags = options.version >= 2;
        const bool has_directory_offset = options.version >= 3;

        godot_bytes image;
        append_u32(image, godot_pck_magic);
        append_u32(image, options.version);
        append_u32(image, options.engine_version[0]);
        append_u32(image, options.engine_version[1]);
        append_u32(image, options.engine_version[2]);
        if (has_flags) {
            append_u32(image, options.pack_flags);
            append_u64(image, options.file_base);
        }
        if (has_directory_offset) {
            const std::uint64_t stored_directory = options.stored_directory_offset.value_or(0);
            append_u64(image, stored_directory);
        } else {
            image.resize(image.size() + 64, 0);
        }
        const std::uint64_t directory_position = image.size();

        std::vector<std::vector<std::uint8_t>> names;
        names.reserve(options.entries.size());
        std::transform(options.entries.begin(), options.entries.end(), std::back_inserter(names), stored_name);

        std::size_t directory_size = 4;
        for (const auto &name : names) {
            directory_size += 4 + name.size() + 8 + 8 + 16;
            if (has_flags) {
                directory_size += 4;
            }
        }
        const std::uint64_t data_start = directory_position + directory_size;

        const bool relative_base = has_directory_offset || (options.pack_flags & godot_pack_flag_relative_base) != 0;
        std::uint64_t base = 0;
        if (has_flags) {
            base = relative_base ? options.image_offset + options.file_base : options.file_base;
        }

        godot_bytes directory;
        const std::uint32_t count = options.entry_count.value_or(static_cast<std::uint32_t>(options.entries.size()));
        append_u32(directory, count);
        std::uint64_t cursor = data_start;
        for (std::size_t index = 0; index < options.entries.size(); ++index) {
            const godot_entry_options &entry = options.entries[index];
            append_u32(directory, static_cast<std::uint32_t>(names[index].size()));
            std::copy(names[index].begin(), names[index].end(), std::back_inserter(directory));
            const std::uint64_t stored_offset = entry.stored_offset.value_or((options.image_offset + cursor) - base);
            const std::uint64_t stored_size = entry.stored_size.value_or(entry.payload.size());
            append_u64(directory, stored_offset);
            append_u64(directory, stored_size);
            std::copy(entry.md5.begin(), entry.md5.end(), std::back_inserter(directory));
            if (has_flags) {
                append_u32(directory, entry.flags);
            }
            cursor += entry.payload.size();
        }

        if (has_directory_offset && !options.stored_directory_offset.has_value()) {
            patch_u64(image, image.size() - 8, directory_position);
        }

        godot_bytes result = std::move(image);
        result.insert(result.end(), directory.begin(), directory.end());
        for (const auto &entry : options.entries) {
            result.insert(result.end(), entry.payload.begin(), entry.payload.end());
        }
        return result;
    }

    godot_bytes build_godot_pe(const godot_pe_section &section)
    {
        const std::uint32_t pointer = section.pointer.value_or(static_cast<std::uint32_t>(fixture_raw_offset));
        const std::uint32_t declared = section.declared_size.value_or(static_cast<std::uint32_t>(section.data.size()));
        const std::uint16_t count = section.section_count.value_or(1);
        const std::uint16_t optional_size = section.optional_size.value_or(fixture_optional_size);

        godot_bytes result(fixture_raw_offset, 0);
        result.insert(result.end(), section.data.begin(), section.data.end());

        result[0] = static_cast<std::uint8_t>('M');
        result[1] = static_cast<std::uint8_t>('Z');
        patch_u32(result, 0x3C, fixture_pe_offset);
        patch_u32(result, fixture_pe_offset, 0x00004550U);
        patch_u16(result, fixture_pe_offset + 4, 0x8664);
        patch_u16(result, fixture_pe_offset + 6, count);
        patch_u16(result, fixture_pe_offset + 20, optional_size);
        patch_u16(result, fixture_pe_offset + 22, 0x0022);

        write_section_name(result, fixture_section_table, section.name);
        patch_u32(result, fixture_section_table + 8, declared);
        patch_u32(result, fixture_section_table + 12, 0x1000);
        patch_u32(result, fixture_section_table + 16, declared);
        patch_u32(result, fixture_section_table + 20, pointer);
        return result;
    }

    godot_bytes minimal_pe_without_pack()
    {
        godot_pe_section section;
        section.name = "data";
        section.data.assign(16, 0);
        return build_godot_pe(section);
    }

    godot_bytes wrap_godot_pck_in_pe(godot_pck_options options, const std::size_t section_lead,
                                     const std::optional<std::uint32_t> declared_size)
    {
        options.image_offset = fixture_raw_offset + section_lead;
        const godot_bytes image = build_godot_pck(options);

        godot_pe_section section;
        section.name = "pck";
        section.data.assign(section_lead, 0);
        section.data.insert(section.data.end(), image.begin(), image.end());
        section.declared_size = declared_size;
        return build_godot_pe(section);
    }

    godot_bytes wrap_godot_pck_in_footer(godot_pck_options options, const std::size_t prefix_size)
    {
        options.image_offset = prefix_size;
        const godot_bytes image = build_godot_pck(options);

        // The prefix is neutral filler ahead of the pack. It must not be a run of zero bytes:
        // the zanzarah module's all-zero signature would then also claim the file at the observer
        // module gate, making the container ambiguous instead of isolating the Godot module.
        constexpr std::uint8_t prefix_fill = 0xAA;
        godot_bytes result(prefix_size, prefix_fill);
        result.insert(result.end(), image.begin(), image.end());
        append_u64(result, image.size());
        append_u32(result, godot_pck_magic);
        return result;
    }
} // namespace test::support
