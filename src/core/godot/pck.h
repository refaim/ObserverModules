#pragma once

// Platform-neutral Reader for Godot PCK packs (versions 0-4) plus the standalone, PE
// embedded-section and trailing-footer location strategies.
//
// The reader is deliberately value-oriented and standard-library only: it depends on the
// bounded core stream (never Observer or Win32) and resolves every entry to an absolute,
// checked physical byte range without loading script, resource or external data.

#include <array>
#include <cstddef>
#include <cstdint>
#include <istream>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include "../codecs/crypto_codec.h"

namespace unpackrat::godot
{
    struct byte_range final
    {
        std::uint64_t offset = 0;
        std::uint64_t size = 0;
    };

    enum class container_kind : std::uint8_t
    {
        standalone,
        pe_section,
        footer,
    };

    struct pack_location final
    {
        byte_range pack{};
        container_kind container = container_kind::standalone;
    };

    enum class pck_entry_kind : std::uint8_t
    {
        file,
        removal,
    };

    struct pck_entry final
    {
        std::string path;
        byte_range body{};
        std::array<std::byte, 16> md5{};
        // body is the exact physical envelope for encrypted members; logical_size describes
        // the decrypted file. A removal record is metadata, never an ordinary empty file.
        std::uint64_t logical_size = 0;
        pck_entry_kind kind = pck_entry_kind::file;
        bool encrypted = false;
    };

    struct pck_index final
    {
        std::uint32_t format_version = 0;
        std::array<std::uint32_t, 3> engine_version{};
        std::vector<pck_entry> entries;
        bool key_evidence = false;
    };

    // Aggregate ceiling for directory metadata. It bounds the entry-vector storage plus the
    // cumulative stored and transient entry-name/path bytes, so a structurally plausible but
    // hostile directory cannot drive a large allocation before it is validated.
    struct pck_limits final
    {
        std::uint64_t max_index_bytes = 64ULL * 1024ULL * 1024ULL;
    };

    class pck_error : public std::runtime_error
    {
      public:
        using std::runtime_error::runtime_error;
    };

    class pck_key_required final : public pck_error
    {
      public:
        pck_key_required(const char *message, const std::uint64_t bytes) : pck_error(message), validation_bytes(bytes)
        {
        }

        // Full encrypted-directory ciphertext cost; charged before each candidate is tried.
        const std::uint64_t validation_bytes;
    };

    // Locates a pack inside the stream. A non-pack stream yields std::nullopt; a stream that
    // structurally claims a pack but is malformed throws pck_error.
    [[nodiscard]] std::optional<pack_location> locate_pack(std::istream &stream);

    // Reads the directory of a located pack, resolving each entry to an absolute physical
    // byte range. Malformed or unsupported packs throw pck_error. ``limits`` bounds the total
    // directory metadata the reader is willing to allocate.
    [[nodiscard]] pck_index read_pack(std::istream &stream, const pack_location &location,
                                      const pck_limits &limits = {}, const codecs::key256 *key = nullptr);
} // namespace unpackrat::godot
