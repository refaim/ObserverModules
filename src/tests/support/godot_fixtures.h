#ifndef UNPACKRAT_TESTS_SUPPORT_GODOT_FIXTURES_H
#define UNPACKRAT_TESTS_SUPPORT_GODOT_FIXTURES_H

// Repository-owned Godot PCK fixture builder. Every byte is generated here; no external
// archive or engine artefact is read. The builders intentionally expose the raw header
// fields (base, flags, directory offset, section headers) so both well-formed packs and
// deliberately malformed ones can be produced without ad-hoc byte surgery in each test.

#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace test::support
{
    using godot_bytes = std::vector<std::uint8_t>;

    // Godot's packed-file magic header: "GDPC" in little-endian byte order.
    inline constexpr std::uint32_t godot_pck_magic = 0x43504447U;
    inline constexpr std::uint32_t godot_pack_flag_relative_base = 1U << 1U;

    struct godot_entry_options final
    {
        std::string path;
        std::vector<std::uint8_t> payload;
        std::optional<std::uint64_t> stored_offset;
        std::optional<std::uint64_t> stored_size;
        std::array<std::uint8_t, 16> md5{};
        std::uint32_t flags = 0;
        // When set, the directory stores exactly these name bytes (length = size()) instead of
        // the official padded encoding of ``path``. Used to exercise unpadded historical fields
        // and deliberately malformed padding.
        std::optional<std::vector<std::uint8_t>> raw_name;
    };

    struct godot_pck_options final
    {
        std::uint32_t version = 4;
        std::array<std::uint32_t, 3> engine_version{4, 0, 0};
        std::uint32_t pack_flags = 0;
        std::uint64_t file_base = 0;
        // Absolute offset at which the produced image will be placed inside a larger file.
        std::uint64_t image_offset = 0;
        std::optional<std::uint64_t> stored_directory_offset;
        std::optional<std::uint32_t> entry_count;
        std::vector<godot_entry_options> entries;
    };

    // Builds a PCK image that begins with the "GDPC" magic.
    [[nodiscard]] godot_bytes build_godot_pck(const godot_pck_options &options);

    struct godot_pe_section final
    {
        std::string name = "pck";
        std::vector<std::uint8_t> data;
        std::optional<std::uint16_t> section_count;
        std::optional<std::uint16_t> optional_size;
        std::optional<std::uint32_t> pointer;
        std::optional<std::uint32_t> declared_size;
    };

    // Builds a minimal PE executable carrying one section with the supplied raw data.
    [[nodiscard]] godot_bytes build_godot_pe(const godot_pe_section &section);

    // A minimal PE executable whose only section is not named "pck".
    [[nodiscard]] godot_bytes minimal_pe_without_pack();

    // Wraps a freshly built PCK image in a PE "pck" section. ``section_lead`` inserts neutral
    // filler bytes before the image so the header alignment behaviour can be exercised.
    [[nodiscard]] godot_bytes wrap_godot_pck_in_pe(godot_pck_options options, std::size_t section_lead = 0,
                                                   std::optional<std::uint32_t> declared_size = std::nullopt);

    // Wraps a freshly built PCK image in the official trailing footer.
    [[nodiscard]] godot_bytes wrap_godot_pck_in_footer(godot_pck_options options, std::size_t prefix_size);

    void patch_u16(godot_bytes &bytes, std::size_t offset, std::uint16_t value);
    void patch_u32(godot_bytes &bytes, std::size_t offset, std::uint32_t value);
    void patch_u64(godot_bytes &bytes, std::size_t offset, std::uint64_t value);
} // namespace test::support
#endif
