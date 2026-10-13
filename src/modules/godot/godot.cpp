#include "../../core/archive_limits.h"
#include "../../core/godot/import_remap.h"
#include "../../core/godot/pck.h"
#include "../extractor.h"
#include "archive_session.h"
#include "core/godot/encrypted_payload.h"
#include "resource_converter.h"
#include "settings.h"
#include "virtual_tree.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <istream>
#include <iterator>
#include <limits>
#include <memory>
#include <numeric>
#include <optional>
#include <ostream>
#include <span>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

// The Godot PCK Observer adapter. It locates a pack inside a standalone, PE-section or trailing
// footer container or a same-stem sidecar. Each physical source has one visible view:
// its decoded resource when enabled and supported, otherwise its logical original.
// Exact converted sizes may require a bounded sizing pass; extraction uses the session source.
//
// build/unpackrat-module-version.props injects the version macros from src/modules/godot/VERSION,
// the single source of truth. A project that does not import that file fails to compile here.

namespace
{
    // Observer v6 has one module-load settings block; each opened archive snapshots this value.
    std::atomic_bool decode_resources_option{false};
} // namespace
namespace extractor
{
    namespace
    {
        namespace godot = unpackrat::godot;
        namespace archive_limits = unpackrat::archive_limits;

        constexpr std::uint64_t copy_chunk_bytes = std::uint64_t{64} * 1024;
        constexpr std::uint64_t max_metadata_body_bytes = 4ULL * 1024 * 1024;

        // Aggregate alias-pass bounds for a single listing. The physical listing is always
        // complete; these ceilings only end the optional import/remap pass early, or make the
        // presentation tree fall back to a smaller alias prefix, so an adversarially amplified alias
        // set can never lose the raw listing.
        //
        // Two separate ceilings sit deliberately apart. ``max_metadata_input_bytes`` is the
        // transient ceiling: the attempted metadata input, malformed fragments included, is charged
        // before a fragment's body is read, and the conservative logical parse peak of one fragment
        // is checked against the residual transient ceiling before the body is read. The retained
        // ``max_alias_owned_bytes`` is checked exactly once per fragment against the residual
        // retained ceiling, so an exact overflow can still happen independently of the peak check
        // and is half of the virtual tree's own 64 MiB metadata budget.
        constexpr std::uint64_t max_metadata_input_bytes = std::uint64_t{64} * 1024 * 1024;
        constexpr std::size_t parser_mapping_ceiling = godot::metadata_limits{}.max_mappings;
        constexpr std::size_t max_alias_owned_bytes = godot::tree_limits{}.max_metadata_bytes / 2;
        constexpr std::size_t max_alias_mappings = godot::tree_limits{}.max_entries;

        // Every retained mapping is charged at least one ``import_mapping`` slot, so the retained
        // byte ceiling already bounds the alias count by the tree entry limit; the explicit count
        // predicates this pass used to carry were unreachable on both x86 and x64.
        static_assert(max_alias_owned_bytes / sizeof(godot::import_mapping) <= max_alias_mappings,
                      "the retained byte ceiling must already bound the alias count by the entry limit");

        // Conservative logical peak, in bytes, of parsing one eligible fragment. Four body-sized
        // buffers are live at once while the mappings are built: the stored input bytes, the reader's
        // retained ``found`` (left alive because ``scan`` returns its result by copy), the returned
        // ``found`` copy, and the normalised ``virtual_path`` clone. String and vector growth double
        // that, so eight body lengths cover the live copies plus allocator and scratch headroom. Each
        // of the fragment's up to ``parser_mapping_ceiling`` mappings also clones the bounded source
        // path and takes one retained slot, and the mapping vector's growth can reallocate a second
        // time, so that allowance is doubled as well.
        constexpr std::uint64_t body_peak_factor = 8;

        std::uint64_t fragment_peak_bytes(const std::uint64_t body, const std::size_t path_clone)
        {
            const std::uint64_t copies = body_peak_factor * body;
            const std::uint64_t retained =
                static_cast<std::uint64_t>(parser_mapping_ceiling) * path_clone +
                static_cast<std::uint64_t>(parser_mapping_ceiling) * sizeof(godot::import_mapping);
            return copies + 2 * retained;
        }

        bool is_metadata_path(const std::string_view path) noexcept
        {
            return path.ends_with(".import") || path.ends_with(".remap");
        }

        // Reads exactly ``count`` bytes at ``offset`` from the archive-owned source stream.
        // archive::open enables failbit/badbit exceptions on that ifstream; a failed seek or
        // short read therefore reaches the catch below, without a second state/gcount check.
        void read_source(std::istream &in, const std::uint64_t offset, char *destination, const std::size_t count)
        {
            try {
                in.clear();
                in.seekg(static_cast<std::streamoff>(offset));
                in.read(destination, static_cast<std::streamsize>(count));
            } catch (const std::ios_base::failure &) {
                throw read_error();
            }
        }

        // Lazy handler for a raw source-backed entry: it copies the stored window in bounded chunks
        // and reports the actual bytes of each chunk. A source fault becomes a read error; an output
        // stream failure is left to escape unchanged.
        void copy_source(std::istream &in, std::ostream &out, const std::uint64_t offset, const std::uint64_t size,
                         const std::function<void(int64_t)> &report_progress)
        {
            std::uint64_t position = offset;
            std::uint64_t remaining = size;
            std::vector<char> buffer(static_cast<std::size_t>(copy_chunk_bytes));
            while (remaining > 0) {
                const std::uint64_t chunk = std::min(remaining, copy_chunk_bytes);
                const auto byte_count = static_cast<std::size_t>(chunk);
                read_source(in, position, buffer.data(), byte_count);
                out.write(buffer.data(), static_cast<std::streamsize>(byte_count));
                position += chunk;
                remaining -= chunk;
                report_progress(static_cast<int64_t>(chunk));
            }
        }

        // Lazy handler for a converted view: the dispatcher re-reads the fresh source and streams the
        // converted bytes. The progress poll runs before every sink write so a cancellation aborts
        // before touching the destination, and the stored source byte count is reported once the
        // conversion has succeeded. A source fault becomes a read error; the sink's own exception
        // (including an output write failure) escapes unchanged.
        void write_converted(std::istream &in, std::ostream &out, const godot::byte_range source,
                             const godot::resource_descriptor &descriptor, const int64_t packed_size,
                             const std::function<void(int64_t)> &report_progress)
        {
            const auto sink = [&out, &report_progress](const std::span<const std::byte> chunk) {
                report_progress(0);
                out.write(reinterpret_cast<const char *>(chunk.data()), static_cast<std::streamsize>(chunk.size()));
            };
            try {
                godot::write_resource(in, source, descriptor, sink);
            } catch (const godot::resource_source_error &) {
                throw read_error();
            }
            report_progress(packed_size);
        }

        // Lazy handler for a generated metadata entry: it writes its own text and reports its own
        // byte count. An output stream failure escapes unchanged.
        void write_generated(std::ostream &out, const std::string &text,
                             const std::function<void(int64_t)> &report_progress)
        {
            out.write(text.data(), static_cast<std::streamsize>(text.size()));
            report_progress(static_cast<int64_t>(text.size()));
        }

        class entry_source final
        {
          public:
            entry_source(godot::archive_session &session, const std::size_t position, const bool verify,
                         const std::function<void(int64_t)> &progress = {})
                : stream_(&session.stream()), range_(session.index().entries[position].body)
            {
                const auto &entry = session.index().entries[position];
                if (!entry.encrypted)
                    return;
                // The session admits nonempty encrypted members only after accepting a verified
                // key. Empty members need no key; all callers use this session's immutable index.
                const auto *key = session.key();
                const unpackrat::codecs::key256 empty_key{};
                try {
                    const auto payload = godot::read_encrypted_payload(session.stream(), entry.body);
                    encrypted_ = std::make_unique<godot::verified_payload_source>(session.stream(), payload,
                                                                                  key == nullptr ? empty_key : *key);
                    if (verify) {
                        encrypted_->verify([&progress](std::uint64_t) {
                            if (progress)
                                progress(0);
                        });
                        stream_ = &encrypted_->stream();
                    } else {
                        stream_ = &encrypted_->unverified_stream();
                    }
                    range_ = {0, entry.logical_size};
                } catch (const godot::encryption_error &) {
                    throw read_error();
                }
            }
            std::istream &stream() noexcept
            {
                return *stream_;
            }
            godot::byte_range range() const noexcept
            {
                return range_;
            }

          private:
            std::istream *stream_;
            godot::byte_range range_;
            std::unique_ptr<godot::verified_payload_source> encrypted_;
        };

        std::vector<std::byte> read_metadata_body(godot::archive_session &session, const std::size_t position)
        {
            const auto &entry = session.index().entries[position];
            entry_source source(session, position, true);
            std::vector<std::byte> bytes(static_cast<std::size_t>(entry.logical_size));
            if (!bytes.empty()) {
                read_source(source.stream(), source.range().offset, reinterpret_cast<char *>(bytes.data()),
                            bytes.size());
            }
            return bytes;
        }

        // Builds the presentation tree from the admitted alias prefix. A metadata-budget breach
        // retries with a halved whole-fragment prefix, so optional alias metadata can never lose the
        // raw listing: every retry drops at least one admitted fragment, the empty prefix (raw only)
        // is the last fallback, and a breach there is the genuine raw-listing failure and is
        // rethrown. Any other tree error is a structural fault and propagates immediately. The
        // returned prefix is the first that fits; it is not claimed to be maximal, because dropping a
        // mapping changes relocated paths and generated manifest cost, so cost is not monotone in the
        // prefix length. Every failed attempt lets its partial tree unwind, so nothing is retained.
        godot::virtual_tree build_bounded_tree(const godot::pck_index &index,
                                               const std::vector<godot::import_mapping> &mappings,
                                               const std::span<const godot::conversion_hint> hints,
                                               const std::vector<std::size_t> &checkpoints)
        {
            std::size_t fragments = checkpoints.size();
            for (;;) {
                const std::size_t usable = fragments == 0 ? 0 : checkpoints[fragments - 1];
                const std::span<const godot::import_mapping> view(mappings.data(), usable);
                try {
                    return godot::make_virtual_tree(index, view, hints);
                } catch (const godot::tree_budget_error &) {
                    if (fragments == 0) {
                        throw;
                    }
                    fragments /= 2;
                }
            }
        }
    } // namespace

    void configure(const wchar_t *settings) noexcept
    {
        decode_resources_option.store(godot::parse_module_settings(settings).decode_resources);
    }
    void reset_settings() noexcept
    {
        decode_resources_option.store(false);
    }

    version_info get_version_info() noexcept
    {
        return {
            {0x8f3e2b1a, 0x4c6d, 0x4e2f, {0xa1, 0xb2, 0xc3, 0xd4, 0xe5, 0xf6, 0x07, 0x18}},
            UNPACKRAT_MODULE_VERSION_MAJOR,
            UNPACKRAT_MODULE_VERSION_MINOR,
        };
    }

    // An empty signature accepts every standalone pack, PE executable and trailing-file footer so
    // the archive layer hands the whole file to the pack locator instead of pre-filtering it.
    std::vector<std::byte> extractor::get_signature()
    {
        return {};
    }

    archive_info extractor::get_archive_info(std::istream &stream, const std::span<const std::byte> &data,
                                             const std::filesystem::path &path, const std::string_view password)
    {
        static_cast<void>(stream);
        static_cast<void>(data);
        state_ = std::make_unique<godot::archive_session>(path, password, godot::session_limits{},
                                                          decode_resources_option.load());
        return archive_info{L"Godot", L"", L""};
    }

    std::vector<std::unique_ptr<file>> extractor::list_files(std::istream &stream)
    {
        static_cast<void>(stream);
        // archive::prepare_files enforces the successful-open precondition, including after
        // a failed reopen; get_archive_info has already installed this owned session.
        auto &session = static_cast<godot::archive_session &>(*state_);
        const godot::pck_index &index = session.index();

        // The bounded import/remap metadata of every stored ".import"/".remap" entry becomes an
        // alias mapping. A malformed fragment is ignored rather than failing the whole listing, so
        // every physical source stays reachable regardless. The whole pass shares one aggregate
        // allowance: once the attempted input or the retained alias set is exhausted the pass
        // simply stops, leaving the remaining metadata fragments as raw entries.
        std::vector<godot::import_mapping> mappings;
        std::vector<std::size_t> checkpoints;
        std::uint64_t metadata_input_bytes = 0;
        std::size_t alias_owned_bytes = 0;
        for (std::size_t metadata_position = 0; metadata_position < index.entries.size(); ++metadata_position) {
            const auto &entry = index.entries[metadata_position];
            if (entry.kind != godot::pck_entry_kind::file || !is_metadata_path(entry.path) ||
                entry.logical_size > max_metadata_body_bytes) {
                continue;
            }

            // Attempted input is charged before the body is read, so a run of large fragments,
            // malformed ones included, cannot drive unbounded reads.
            if (entry.logical_size > max_metadata_input_bytes - metadata_input_bytes) {
                break;
            }
            metadata_input_bytes += entry.logical_size;

            // Reject a fragment whose conservative logical parse peak no longer fits the residual
            // transient ceiling, so the live copies, cloned paths and mapping-vector growth during
            // the parse stay bounded even when the retained result itself would be small.
            const std::size_t path_clone = static_cast<std::size_t>(std::min<std::uint64_t>(
                entry.logical_size, static_cast<std::uint64_t>(archive_limits::max_path_bytes)));
            if (fragment_peak_bytes(entry.logical_size, path_clone) > max_metadata_input_bytes - alias_owned_bytes) {
                break;
            }

            const std::vector<std::byte> bytes = read_metadata_body(session, metadata_position);
            try {
                std::vector<godot::import_mapping> parsed = godot::read_import_remap(bytes, entry.path);
                // Exact retained charge: the parsed fragments' own strings plus one mapping slot
                // each, checked against the residual retained ceiling before a single one is moved
                // into the shared vector.
                const std::size_t fragment_bytes =
                    std::accumulate(parsed.begin(), parsed.end(), std::size_t{0},
                                    [](const std::size_t total, const godot::import_mapping &mapping) {
                                        return total + mapping.virtual_path.size() + mapping.compiled_path.size() +
                                               mapping.variant.size() + sizeof(godot::import_mapping);
                                    });
                if (fragment_bytes > max_alias_owned_bytes - alias_owned_bytes) {
                    break;
                }
                alias_owned_bytes += fragment_bytes;
                mappings.insert(mappings.end(), std::make_move_iterator(parsed.begin()),
                                std::make_move_iterator(parsed.end()));
                checkpoints.push_back(mappings.size());
            } catch (const godot::metadata_error &) {
                continue;
            }
        }

        // Recognise resource headers without decoding media or computing encoded output sizes.
        // A declined conversion keeps its original; disabled mode performs no resource probes.
        std::vector<godot::conversion_hint> hints;
        std::unordered_map<std::size_t, godot::resource_descriptor> descriptors;
        if (session.decode_resources()) {
            for (std::size_t position = 0; position < index.entries.size(); ++position) {
                const auto &entry = index.entries[position];
                if (entry.kind != godot::pck_entry_kind::file)
                    continue;
                try {
                    entry_source source(session, position, false);
                    auto &input = source.stream();
                    const auto range = source.range();
                    const auto descriptor = godot::probe_resource(input, range);
                    if (descriptor.has_value()) {
                        hints.push_back(godot::conversion_hint{position, descriptor->extension});
                        descriptors.emplace(position, *descriptor);
                    }
                } catch (const godot::resource_source_error &) {
                    throw read_error();
                }
            }
        }

        const godot::virtual_tree tree = build_bounded_tree(index, mappings, hints, checkpoints);

        std::vector<std::unique_ptr<file>> files;
        files.reserve(tree.entries.size());
        for (const godot::virtual_entry &row : tree.entries) {
            auto item = std::make_unique<file>();
            item->path = row.path;
            if (row.source_index.has_value()) {
                const godot::pck_entry &source = index.entries[*row.source_index];
                const std::uint64_t size = source.body.size;
                item->offset = static_cast<int64_t>(source.body.offset);
                item->compressed_body_size_in_bytes = static_cast<int64_t>(size);
                item->uncompressed_body_size_in_bytes = static_cast<int64_t>(source.logical_size);
                const std::optional<godot::resource_descriptor> descriptor =
                    row.converted ? std::optional<godot::resource_descriptor>(descriptors.at(*row.source_index))
                                  : std::nullopt;
                // Converted views deliberately display the packed source size. Output bytes and
                // progress are produced only by extraction, without a discarded conversion pass.
                if (descriptor.has_value())
                    item->uncompressed_body_size_in_bytes = item->compressed_body_size_in_bytes;
                item->extract = [session = &session, source_index = *row.source_index, descriptor = descriptor,
                                 packed_size = item->compressed_body_size_in_bytes](
                                    std::istream &, std::ostream &out, const std::function<void(int64_t)> &progress) {
                    entry_source source(*session, source_index, true, progress);
                    if (descriptor.has_value())
                        write_converted(source.stream(), out, source.range(), *descriptor, packed_size, progress);
                    else
                        copy_source(source.stream(), out, source.range().offset, source.range().size, progress);
                };
            } else {
                const int64_t size = static_cast<int64_t>(row.generated_text.size());
                item->compressed_body_size_in_bytes = size;
                item->uncompressed_body_size_in_bytes = size;
                item->extract = [text = row.generated_text](std::istream &, std::ostream &out,
                                                            const std::function<void(int64_t)> &progress) {
                    write_generated(out, text, progress);
                };
            }
            files.push_back(std::move(item));
        }
        return files;
    }

    uint32_t extractor::decrypt(uint32_t magic, std::vector<char> &data) const
    {
        static_cast<void>(data);
        return magic;
    }
} // namespace extractor
