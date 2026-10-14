#include "pck.h"
#include "encrypted_payload.h"

#include "../archive_limits.h"
#include "../io/bounded_stream.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <string>
#include <string_view>
#include <type_traits>

// Adapted from the canonical Godot packed-file reader, distributed under the MIT license:
//   Copyright (c) 2014-present Godot Engine contributors (see AUTHORS.md).
//   Copyright (c) 2007-2014 Juan Linietsky, Ariel Manzur.
// Sources pinned for traceability:
//   godotengine/godot core/io/file_access_pack.cpp @ ed1daf0bf001b61586d9930840f2f1394092c079
//     (PCK v2, v3 and v4 layouts, flags, file base and directory offset),
//   godotengine/godot core/io/file_access_pack.cpp @ 2.1.6-stable (PCK v0 layout),
//   godotengine/godot core/io/file_access_pack.cpp @ 3.6-stable (PCK v1 layout).
// Only the format logic is reused; the reader is re-implemented over the bounded core stream
// with checked arithmetic, explicit budgets and a strict "reject, never warn-and-continue"
// policy. The MIT notice text ships in licenses/ once the module adapter is complete.

namespace unpackrat::godot
{
    namespace
    {
        constexpr std::uint32_t pack_magic = 0x43504447U; // "GDPC"
        constexpr std::uint32_t max_format_version = 4;
        constexpr std::uint32_t pack_flag_directory_encrypted = 1U << 0U;
        constexpr std::uint32_t pack_flag_relative_base = 1U << 1U;
        constexpr std::uint32_t pack_flag_sparse_bundle = 1U << 2U;
        constexpr std::uint32_t known_pack_flags =
            pack_flag_directory_encrypted | pack_flag_relative_base | pack_flag_sparse_bundle;
        constexpr std::uint32_t file_flag_encrypted = 1U << 0U;
        constexpr std::uint32_t file_flag_removal = 1U << 1U;
        constexpr std::uint32_t file_flag_delta = 1U << 2U;
        constexpr std::uint32_t known_file_flags = file_flag_encrypted | file_flag_removal | file_flag_delta;

        constexpr std::uint16_t dos_signature = 0x5A4DU;    // "MZ"
        constexpr std::uint32_t pe_signature = 0x00004550U; // "PE\0\0"
        constexpr std::uint16_t section_header_size = 40;
        constexpr std::uint32_t alignment_search_span = 8;
        constexpr std::size_t reserved_header_bytes = 64;
        constexpr std::uint64_t minimum_pe_header = 0x40;

        // The official packer stores each entry name length padded up to a 4-byte boundary and
        // fills the remainder with NUL bytes, so a valid stored name may carry up to three
        // trailing NULs. The logical path limit stays archive_limits::max_path_bytes.
        constexpr std::size_t max_name_padding = 3;

        [[nodiscard]] std::uint64_t checked_add(const std::uint64_t left, const std::uint64_t right)
        {
            const std::uint64_t sum = left + right;
            if (sum < left) {
                throw pck_error("pck offset arithmetic overflow");
            }
            return sum;
        }

        // Reads strictly inside ``[start, start + size)`` for one located pack, so a header or
        // directory field can never spill past the validated pack range even though the pack
        // itself lives inside a larger physical file.
        class pack_reader final
        {
          public:
            pack_reader(io::bounded_stream &reader, const std::uint64_t limit) : reader_(reader), limit_(limit)
            {
            }

            void seek(const std::uint64_t absolute)
            {
                if (absolute > limit_) {
                    throw pck_error("pack access outside the located range");
                }
                reader_.seek_absolute(static_cast<std::streamoff>(absolute));
            }

            void read(char *destination, const std::size_t byte_count)
            {
                const std::uint64_t cursor = static_cast<std::uint64_t>(reader_.position());
                if (byte_count > limit_ - cursor) {
                    throw pck_error("pack access outside the located range");
                }
                reader_.read_exact(destination, byte_count);
            }

            template <typename Value>
                requires std::is_trivially_copyable_v<Value>
            [[nodiscard]] Value read_trivial()
            {
                Value value{};
                read(reinterpret_cast<char *>(&value), sizeof(value));
                return value;
            }

            [[nodiscard]] std::uint64_t position()
            {
                return static_cast<std::uint64_t>(reader_.position());
            }

          private:
            io::bounded_stream &reader_;
            std::uint64_t limit_;
        };

        void skip_reserved(pack_reader &reader)
        {
            std::array<std::byte, reserved_header_bytes> reserved{};
            reader.read(reinterpret_cast<char *>(reserved.data()), reserved.size());
        }

        std::optional<pack_location> locate_embedded_pack(io::bounded_stream &reader, const std::uint64_t file_size,
                                                          const std::uint32_t raw_offset, const std::uint32_t raw_size,
                                                          const std::uint32_t virtual_size)
        {
            if (raw_offset >= file_size) {
                throw pck_error("pck section offset out of range");
            }
            const std::uint64_t limit = static_cast<std::uint64_t>(raw_offset) + raw_size;
            if (limit > file_size) {
                throw pck_error("pck section range exceeds the file");
            }
            // Godot's PE template reserves an eight-byte zero symbol in a file-aligned section.
            // It is not an embedded pack; a real trailing footer may still carry one.
            if (virtual_size == 8 && raw_size == 512) {
                std::array<std::byte, 512> placeholder{};
                reader.seek_absolute(static_cast<std::streamoff>(raw_offset));
                reader.read_exact(reinterpret_cast<char *>(placeholder.data()), placeholder.size());
                if (std::all_of(placeholder.begin(), placeholder.end(),
                                [](const std::byte byte) { return byte == std::byte{0}; })) {
                    return std::nullopt;
                }
            }
            for (std::uint32_t shift = 0; shift < alignment_search_span; ++shift) {
                const std::uint64_t position = static_cast<std::uint64_t>(raw_offset) + shift;
                if (position + 4 > limit) {
                    break;
                }
                reader.seek_absolute(static_cast<std::streamoff>(position));
                if (reader.read_trivial<std::uint32_t>() == pack_magic) {
                    return pack_location{{position, limit - position}, container_kind::pe_section};
                }
            }
            throw pck_error("pck section does not contain a pack header");
        }

        std::optional<pack_location> locate_pe_section(io::bounded_stream &reader, const std::uint64_t file_size)
        {
            if (file_size < 2) {
                return std::nullopt;
            }
            reader.seek_absolute(0);
            if (reader.read_trivial<std::uint16_t>() != dos_signature) {
                return std::nullopt;
            }
            if (file_size < minimum_pe_header) {
                throw pck_error("truncated PE header");
            }
            reader.seek_absolute(0x3C);
            const std::uint32_t pe_offset = reader.read_trivial<std::uint32_t>();
            if (pe_offset > file_size || file_size - pe_offset < 24) {
                throw pck_error("PE header offset out of range");
            }
            reader.seek_absolute(pe_offset);
            if (reader.read_trivial<std::uint32_t>() != pe_signature) {
                return std::nullopt;
            }

            const std::uint64_t nt_header = pe_offset;
            reader.seek_absolute(static_cast<std::streamoff>(nt_header + 6));
            const std::uint16_t section_count = reader.read_trivial<std::uint16_t>();
            reader.seek_absolute(static_cast<std::streamoff>(nt_header + 20));
            const std::uint16_t optional_size = reader.read_trivial<std::uint16_t>();

            const std::uint64_t table = nt_header + 24 + optional_size;
            if (table > file_size || (file_size - table) / section_header_size < section_count) {
                throw pck_error("PE section table out of range");
            }
            for (std::uint16_t index = 0; index < section_count; ++index) {
                const std::uint64_t header = table + static_cast<std::uint64_t>(index) * section_header_size;
                reader.seek_absolute(static_cast<std::streamoff>(header));
                std::array<char, 8> name{};
                reader.read_exact(name.data(), name.size());
                const std::string_view name_view(name.data(), name.size());
                if (name_view.substr(0, name_view.find('\0')) != "pck") {
                    continue;
                }
                reader.seek_absolute(static_cast<std::streamoff>(header + 8));
                const std::uint32_t virtual_size = reader.read_trivial<std::uint32_t>();
                reader.seek_absolute(static_cast<std::streamoff>(header + 16));
                const std::uint32_t raw_size = reader.read_trivial<std::uint32_t>();
                const std::uint32_t raw_offset = reader.read_trivial<std::uint32_t>();
                if (raw_offset == 0) {
                    return std::nullopt;
                }
                return locate_embedded_pack(reader, file_size, raw_offset, raw_size, virtual_size);
            }
            return std::nullopt;
        }

        std::optional<pack_location> locate_footer(io::bounded_stream &reader, const std::uint64_t file_size)
        {
            if (file_size < 4) {
                return std::nullopt;
            }
            reader.seek_absolute(static_cast<std::streamoff>(file_size - 4));
            if (reader.read_trivial<std::uint32_t>() != pack_magic) {
                return std::nullopt;
            }
            if (file_size < 12) {
                throw pck_error("truncated pck footer");
            }
            reader.seek_absolute(static_cast<std::streamoff>(file_size - 12));
            const std::uint64_t payload_size = reader.read_trivial<std::uint64_t>();
            if (payload_size < 4 || payload_size > file_size - 12) {
                throw pck_error("invalid embedded pck size");
            }
            const std::uint64_t start = file_size - 12 - payload_size;
            reader.seek_absolute(static_cast<std::streamoff>(start));
            if (reader.read_trivial<std::uint32_t>() != pack_magic) {
                throw pck_error("embedded pck header missing");
            }
            return pack_location{{start, payload_size}, container_kind::footer};
        }

        std::optional<pack_location> locate_pack_impl(std::istream &stream)
        {
            io::bounded_stream reader(stream);
            // The non-negative streamoff size bounds every validated locator position, so the
            // explicit seek casts above stay representable in the stream's signed offset type.
            const std::uint64_t file_size = static_cast<std::uint64_t>(reader.size());
            if (file_size >= 4) {
                reader.seek_absolute(0);
                if (reader.read_trivial<std::uint32_t>() == pack_magic) {
                    return pack_location{{0, file_size}, container_kind::standalone};
                }
            }
            if (const std::optional<pack_location> embedded = locate_pe_section(reader, file_size)) {
                return embedded;
            }
            return locate_footer(reader, file_size);
        }

        std::string sanitize_path(std::string_view raw)
        {
            if (raw.find('\0') != std::string_view::npos) {
                throw pck_error("pack entry name contains NUL");
            }
            if (raw.starts_with("res://")) {
                raw.remove_prefix(6);
            }
            if (raw.empty()) {
                throw pck_error("pack entry name is empty");
            }
            if (raw.front() == '/') {
                throw pck_error("pack entry name is absolute");
            }
            if (raw.find_first_of("\\:") != std::string_view::npos) {
                throw pck_error("pack entry name is unsafe");
            }
            std::size_t cursor = 0;
            while (true) {
                const std::size_t slash = raw.find('/', cursor);
                const std::size_t stop = slash == std::string_view::npos ? raw.size() : slash;
                if (raw.substr(cursor, stop - cursor) == "..") {
                    throw pck_error("pack entry name escapes its directory");
                }
                if (slash == std::string_view::npos) {
                    break;
                }
                cursor = slash + 1;
            }
            return std::string(raw);
        }

        // Charges ``amount`` against the remaining metadata budget, rejecting before the caller
        // performs the allocation the charge accounts for.
        std::uint64_t charge_budget(const std::uint64_t used, const std::uint64_t amount, const pck_limits &limits)
        {
            const std::uint64_t total = checked_add(used, amount);
            if (total > limits.max_index_bytes) {
                throw pck_error("pack index exceeds the allocation budget");
            }
            return total;
        }

        // Smallest number of directory bytes a single entry can occupy: the name-length field,
        // at least one stored name byte, the offset, size and MD5, plus the v2+ per-file flags.
        [[nodiscard]] constexpr std::uint64_t minimum_entry_bytes(const std::uint32_t version)
        {
            const std::uint64_t base = 4 + 1 + 8 + 8 + 16;
            return version >= 2 ? base + 4 : base;
        }

        pck_entry read_entry(pack_reader &reader, const std::uint32_t version, const std::uint64_t file_base,
                             const std::uint64_t file_size, const pck_limits &limits, std::uint64_t &budget_used)
        {
            const std::uint32_t name_size = reader.read_trivial<std::uint32_t>();
            if (name_size == 0 || name_size > archive_limits::max_path_bytes + max_name_padding) {
                throw pck_error("pack entry name length out of range");
            }
            budget_used = charge_budget(budget_used, name_size, limits);
            std::string raw(name_size, '\0');
            reader.read(raw.data(), raw.size());

            pck_entry entry;
            const std::uint64_t stored_offset = reader.read_trivial<std::uint64_t>();
            entry.body.size = reader.read_trivial<std::uint64_t>();
            entry.logical_size = entry.body.size;
            reader.read(reinterpret_cast<char *>(entry.md5.data()), entry.md5.size());
            if (version >= 2) {
                const std::uint32_t flags = reader.read_trivial<std::uint32_t>();
                if ((flags & ~known_file_flags) != 0) {
                    throw pck_error("unknown pack entry flag");
                }
                if ((flags & file_flag_delta) != 0 ||
                    (flags & (file_flag_encrypted | file_flag_removal)) == (file_flag_encrypted | file_flag_removal)) {
                    throw pck_error("unsupported pack entry mode");
                }
                entry.encrypted = (flags & file_flag_encrypted) != 0;
                if ((flags & file_flag_removal) != 0) {
                    if (entry.logical_size != 0) {
                        throw pck_error("pack removal record has a file body");
                    }
                    entry.kind = pck_entry_kind::removal;
                }
            }

            // Trim only a validated run of trailing NUL padding; any NUL before it is embedded
            // and is rejected by sanitize_path. The stored length tolerates max_name_padding
            // trailing NULs, so the trimmed logical length is the value that must respect the
            // logical path cap.
            std::size_t logical_size = raw.size();
            while (logical_size > 0 && raw[logical_size - 1] == '\0') {
                --logical_size;
            }
            if (logical_size > archive_limits::max_path_bytes) {
                throw pck_error("pack entry name length out of range");
            }
            budget_used = charge_budget(budget_used, logical_size, limits);
            entry.path = sanitize_path(std::string_view(raw.data(), logical_size));
            entry.body.offset = checked_add(file_base, stored_offset);
            if (checked_add(entry.body.offset, entry.body.size) > file_size) {
                throw pck_error("pack entry range exceeds the file");
            }
            return entry;
        }

        void read_directory(pack_reader &directory, pck_index &index, const std::uint32_t entry_count,
                            const std::uint64_t directory_end, const std::uint64_t file_base,
                            const std::uint64_t file_size, const pck_limits &limits, const std::uint64_t workspace)
        {
            const std::uint64_t directory_remaining = directory_end - directory.position();
            if (static_cast<std::uint64_t>(entry_count) * minimum_entry_bytes(index.format_version) >
                directory_remaining) {
                throw pck_error("pack directory is too small for its entry count");
            }
            std::uint64_t budget_used =
                charge_budget(workspace, static_cast<std::uint64_t>(entry_count) * sizeof(pck_entry), limits);
            index.entries.reserve(entry_count);
            for (std::uint32_t entry_index = 0; entry_index < entry_count; ++entry_index) {
                index.entries.push_back(
                    read_entry(directory, index.format_version, file_base, file_size, limits, budget_used));
            }
        }

        pck_index read_pack_impl(std::istream &stream, const pack_location &location, const pck_limits &limits,
                                 const codecs::key256 *key)
        {
            io::bounded_stream reader(stream);
            const std::uint64_t file_size = static_cast<std::uint64_t>(reader.size());
            const std::uint64_t start = location.pack.offset;
            if (start > file_size || location.pack.size > file_size - start) {
                throw pck_error("pack location exceeds the file");
            }
            const std::uint64_t pack_end = start + location.pack.size;
            pack_reader pack(reader, pack_end);
            pack.seek(start);
            if (pack.read_trivial<std::uint32_t>() != pack_magic) {
                throw pck_error("pack header magic missing");
            }

            pck_index index;
            index.format_version = pack.read_trivial<std::uint32_t>();
            index.engine_version[0] = pack.read_trivial<std::uint32_t>();
            index.engine_version[1] = pack.read_trivial<std::uint32_t>();
            index.engine_version[2] = pack.read_trivial<std::uint32_t>();
            if (index.format_version > max_format_version) {
                throw pck_error("unsupported pack format version");
            }

            std::uint64_t file_base = 0;
            std::uint64_t directory_offset = 0;
            bool encrypted_directory = false;
            if (index.format_version <= 1) {
                skip_reserved(pack);
                directory_offset = pack.position();
            } else {
                const std::uint32_t pack_flags = pack.read_trivial<std::uint32_t>();
                if ((pack_flags & ~known_pack_flags) != 0) {
                    throw pck_error("unknown pack flag");
                }
                encrypted_directory = (pack_flags & pack_flag_directory_encrypted) != 0;
                if ((pack_flags & pack_flag_sparse_bundle) != 0) {
                    throw pck_error("sparse pack bundle is unsupported");
                }
                const std::uint64_t stored_base = pack.read_trivial<std::uint64_t>();
                const bool relative = index.format_version >= 3 || (pack_flags & pack_flag_relative_base) != 0;
                file_base = relative ? checked_add(start, stored_base) : stored_base;
                if (index.format_version >= 3) {
                    const std::uint64_t stored_directory = pack.read_trivial<std::uint64_t>();
                    directory_offset = checked_add(start, stored_directory);
                } else {
                    skip_reserved(pack);
                    directory_offset = pack.position();
                }
            }

            // directory_offset is always >= start, and the pack-bounded reader rejects a seek or
            // read that would leave [start, pack_end), so the directory cannot cross the pack end.
            pack.seek(directory_offset);

            const std::uint32_t entry_count = pack.read_trivial<std::uint32_t>();
            if (entry_count > archive_limits::max_entry_count) {
                throw pck_error("pack entry count exceeds the budget");
            }
            if (encrypted_directory) {
                const std::uint64_t begin = pack.position();
                const encrypted_payload payload = read_encrypted_payload(stream, {begin, pack_end - begin},
                                                                         encryption_limits{limits.max_index_bytes});
                if (key == nullptr && payload.key_evidence()) {
                    throw pck_key_required("encrypted pack directory requires a key", payload.ciphertext.size);
                }
                const std::uint64_t workspace =
                    std::min<std::uint64_t>(payload.ciphertext.size, encryption_chunk_bytes);
                static_cast<void>(
                    charge_budget(workspace, static_cast<std::uint64_t>(entry_count) * sizeof(pck_entry), limits));
                const codecs::key256 empty_key{};
                verified_payload_source source(stream, payload, key == nullptr ? empty_key : *key);
                source.verify();
                index.key_evidence = payload.key_evidence();
                io::bounded_stream logical(source.stream());
                pack_reader directory(logical, payload.logical_size);
                read_directory(directory, index, entry_count, payload.logical_size, file_base, file_size, limits,
                               workspace);
                if (directory.position() != payload.logical_size) {
                    throw pck_error("encrypted pack directory has trailing plaintext");
                }
            } else {
                read_directory(pack, index, entry_count, pack_end, file_base, file_size, limits, 0);
            }
            // Header probing uses the physical stream only after the complete directory has been
            // parsed, so a member seek cannot disturb the plain directory's input cursor.
            for (pck_entry &entry : index.entries) {
                if (entry.encrypted) {
                    // Unsigned subtraction rejects both a pre-pack absolute offset and a
                    // post-pack offset before the remaining source window is derived.
                    const std::uint64_t relative = entry.body.offset - start;
                    if (relative > location.pack.size) {
                        throw pck_error("encrypted member begins outside the located pack");
                    }
                    const encrypted_payload payload =
                        read_encrypted_payload(stream, {entry.body.offset, location.pack.size - relative});
                    if (payload.logical_size != entry.logical_size) {
                        throw pck_error("encrypted member length disagrees with its directory entry");
                    }
                    entry.body = payload.envelope;
                }
            }
            return index;
        }
    } // namespace

    std::optional<pack_location> locate_pack(std::istream &stream)
    {
        try {
            return locate_pack_impl(stream);
        } catch (const io::read_error &) {
            throw pck_error("pack data is truncated or unreadable");
        }
    }

    pck_index read_pack(std::istream &stream, const pack_location &location, const pck_limits &limits,
                        const codecs::key256 *key)
    {
        try {
            return read_pack_impl(stream, location, limits, key);
        } catch (const io::read_error &) {
            throw pck_error("pack data is truncated or unreadable");
        } catch (const key_mismatch &) {
            throw;
        } catch (const encryption_error &error) {
            throw pck_error(error.what());
        }
    }
} // namespace unpackrat::godot
