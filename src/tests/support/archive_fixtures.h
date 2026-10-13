#pragma once

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <optional>
#include <span>
#include <string_view>
#include <vector>

namespace test::support
{
    using byte_buffer = std::vector<std::uint8_t>;

    enum class renpy_version : std::uint8_t
    {
        rpa_2_0,
        rpa_3_0,
    };

    struct renpy_archive_options final
    {
        renpy_version version = renpy_version::rpa_2_0;
        std::uint8_t encryption_key = 0x5a;
        std::optional<std::string_view> header;
        bool include_none_header = false;
    };

    [[nodiscard]] byte_buffer make_renpy_archive(std::string_view path, std::string_view payload);
    [[nodiscard]] byte_buffer make_renpy_archive(std::string_view path, std::string_view payload,
                                                 const renpy_archive_options &options);
    [[nodiscard]] byte_buffer make_renpy_archive_with_index(std::span<const std::uint8_t> pickle_index,
                                                            std::string_view payload = {});
    [[nodiscard]] byte_buffer make_renpy_archive_with_expanded_index(std::size_t expanded_size);
    [[nodiscard]] byte_buffer make_rpgmaker_archive(std::string_view path, std::string_view payload);
    [[nodiscard]] byte_buffer make_zanzarah_archive(std::string_view path, std::string_view payload);
    [[nodiscard]] byte_buffer make_zanzarah_archive_with_entries(std::size_t entry_count);

    struct rpgmaker_entry final
    {
        std::string path;
        std::string payload;
    };

    // Native RPG Maker XP (.rgssad) and VX (.rgss2a) archives share one legacy layout: an eight
    // byte "RGSSAD\0\1" header followed by inline entries whose metadata and bodies are keyed from
    // 0xDEADCAFE. Version 1 is the only legacy revision unpackrat accepts; the tool specific
    // version 2 stays rejected while VX Ace version 3 keeps its separate table format.
    inline constexpr std::uint8_t rpgmaker_legacy_version = 1;

    [[nodiscard]] byte_buffer make_rpgmaker_legacy_header(std::uint8_t version);
    [[nodiscard]] byte_buffer make_rpgmaker_legacy_archive(std::span<const rpgmaker_entry> entries,
                                                           std::uint8_t version = rpgmaker_legacy_version);
    [[nodiscard]] byte_buffer make_rpgmaker_legacy_archive(std::string_view path, std::string_view payload,
                                                           std::uint8_t version = rpgmaker_legacy_version);

    class temporary_archive final
    {
      public:
        temporary_archive(std::string_view label, std::span<const std::uint8_t> contents);
        ~temporary_archive();

        temporary_archive(const temporary_archive &) = delete;
        temporary_archive &operator=(const temporary_archive &) = delete;
        temporary_archive(temporary_archive &&) = delete;
        temporary_archive &operator=(temporary_archive &&) = delete;

        [[nodiscard]] const std::filesystem::path &path() const noexcept;

      private:
        std::filesystem::path path_;
    };

    class temporary_sparse_renpy_archive final
    {
      public:
        temporary_sparse_renpy_archive();
        ~temporary_sparse_renpy_archive();

        temporary_sparse_renpy_archive(const temporary_sparse_renpy_archive &) = delete;
        temporary_sparse_renpy_archive &operator=(const temporary_sparse_renpy_archive &) = delete;
        temporary_sparse_renpy_archive(temporary_sparse_renpy_archive &&) = delete;
        temporary_sparse_renpy_archive &operator=(temporary_sparse_renpy_archive &&) = delete;

        [[nodiscard]] const std::filesystem::path &path() const noexcept;

      private:
        std::filesystem::path path_;
    };
} // namespace test::support
