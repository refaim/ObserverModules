#include "../../core/codecs/ogg_codec.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
#include <limits>
#include <optional>
#include <span>
#include <stdexcept>
#include <vector>

#ifdef _DEBUG
#include <crtdbg.h>
#endif

#include <catch2/catch_test_macros.hpp>

#include <ogg/ogg.h>

TEST_CASE("ogg codec: scalar packet preflight accepts the transport boundary and rejects the next byte",
          "[ogg-packet-range]")
{
    constexpr std::uint64_t transport_max = static_cast<std::uint64_t>((std::numeric_limits<long>::max)());
    CHECK_NOTHROW(unpackrat::codecs::check_ogg_packet_size(transport_max));
    CHECK_THROWS_AS(unpackrat::codecs::check_ogg_packet_size(transport_max + 1), unpackrat::codecs::ogg_error);
}

namespace
{
    using unpackrat::codecs::ogg_error;
    using unpackrat::codecs::ogg_limits;
    using unpackrat::codecs::ogg_packet_view;

    std::vector<std::byte> make_bytes(const std::size_t size, const std::uint32_t seed)
    {
        std::vector<std::byte> data(size);
        std::uint32_t state = seed;
        for (auto &byte : data) {
            state = state * 1664525u + 1013904223u;
            byte = static_cast<std::byte>(state >> 24);
        }
        return data;
    }

    std::vector<std::byte> mux(const std::span<const ogg_packet_view> packets, const ogg_limits &limits = {})
    {
        std::vector<std::byte> output;
        unpackrat::codecs::write_ogg(
            packets,
            [&output](const std::span<const std::byte> chunk) {
                output.insert(output.end(), chunk.begin(), chunk.end());
            },
            limits);
        return output;
    }

    std::uint32_t read_u32_le(const unsigned char *data)
    {
        std::uint32_t value = 0;
        for (int i = 0; i < 4; ++i) {
            value |= static_cast<std::uint32_t>(data[i]) << (8 * i);
        }
        return value;
    }

    // Independently re-derives every page checksum from the raw bytes using the
    // transport library's own checksum routine and manual Ogg framing. This does
    // not depend on the muxer under test.
    void require_valid_pages(const std::span<const std::byte> bytes)
    {
        std::size_t offset = 0;
        std::size_t pages = 0;
        while (offset < bytes.size()) {
            REQUIRE(bytes.size() - offset >= 27);
            const auto *header = reinterpret_cast<const unsigned char *>(bytes.data() + offset);
            REQUIRE(std::memcmp(header, "OggS", 4) == 0);
            REQUIRE(header[4] == 0);

            const auto header_size = std::size_t{27} + header[26];
            REQUIRE(bytes.size() - offset >= header_size);
            // An Ogg page body is the sum of its segment-table lacing values
            // (header[27 .. 27 + segment_count]), not the granule position field
            // at offset 6, which is a 64-bit PCM count that rarely matches.
            std::size_t body_size = 0;
            for (std::size_t segment = 0; segment < static_cast<std::size_t>(header[26]); ++segment) {
                body_size += static_cast<std::size_t>(header[27 + segment]);
            }
            REQUIRE(bytes.size() - offset - header_size >= body_size);
            const auto stored = read_u32_le(header + 22);

            std::vector<unsigned char> scratch(header_size + body_size);
            std::memcpy(scratch.data(), bytes.data() + offset, scratch.size());
            scratch[22] = 0;
            scratch[23] = 0;
            scratch[24] = 0;
            scratch[25] = 0;
            ogg_page page{};
            page.header = scratch.data();
            page.header_len = static_cast<long>(header_size);
            page.body = scratch.data() + header_size;
            page.body_len = static_cast<long>(body_size);
            ogg_page_checksum_set(&page);
            REQUIRE(read_u32_le(scratch.data() + 22) == stored);

            offset += header_size + body_size;
            ++pages;
        }
        REQUIRE(pages != 0);
    }

    struct decoded_stream
    {
        std::vector<std::vector<std::byte>> packets;
        // Per-packet granule positions recovered from the decoded packets, not
        // from the pages. libogg records a page granule only on the last packet
        // of a page, so this is the only way to observe whether a known packet
        // granule actually survived the round trip.
        std::vector<std::int64_t> packet_granules;
        std::vector<long> page_numbers;
        std::vector<int> page_bos;
        std::vector<int> page_eos;
        std::vector<std::int64_t> page_granules;
        std::vector<int> page_serials;
    };

    // RAII ownership for the transport library's decode states. An assertion
    // thrown in the middle of a decode unwinds through these guards, so the
    // library buffers are released instead of leaking into the debug heap.
    class ogg_sync_guard final
    {
      public:
        ogg_sync_guard()
        {
            REQUIRE(ogg_sync_init(&state_) == 0);
        }

        ~ogg_sync_guard()
        {
            // ogg_sync_init always zero-initializes the state, so clearing is
            // safe here even if the constructor's assertion fired.
            static_cast<void>(ogg_sync_clear(&state_));
        }

        ogg_sync_guard(const ogg_sync_guard &) = delete;
        ogg_sync_guard &operator=(const ogg_sync_guard &) = delete;
        ogg_sync_guard(ogg_sync_guard &&) = delete;
        ogg_sync_guard &operator=(ogg_sync_guard &&) = delete;

        [[nodiscard]] ogg_sync_state &handle() noexcept
        {
            return state_;
        }

      private:
        ogg_sync_state state_{};
    };

    class ogg_stream_guard final
    {
      public:
        explicit ogg_stream_guard(const int serial_number)
        {
            REQUIRE(ogg_stream_init(&state_, serial_number) == 0);
        }

        ~ogg_stream_guard()
        {
            // ogg_stream_init zero-initializes the state before allocating, and a
            // failed constructor never reaches this destructor, so clearing is
            // safe here.
            static_cast<void>(ogg_stream_clear(&state_));
        }

        ogg_stream_guard(const ogg_stream_guard &) = delete;
        ogg_stream_guard &operator=(const ogg_stream_guard &) = delete;
        ogg_stream_guard(ogg_stream_guard &&) = delete;
        ogg_stream_guard &operator=(ogg_stream_guard &&) = delete;

        [[nodiscard]] ogg_stream_state &handle() noexcept
        {
            return state_;
        }

      private:
        ogg_stream_state state_{};
    };

    // Decodes the byte stream with the transport library's sync/stream API.
    decoded_stream decode(const std::span<const std::byte> bytes)
    {
        ogg_sync_guard sync;
        std::optional<ogg_stream_guard> stream;
        decoded_stream result;

        std::size_t offset = 0;
        while (offset < bytes.size()) {
            const auto chunk = std::min<std::size_t>(bytes.size() - offset, 2048);
            char *buffer = ogg_sync_buffer(&sync.handle(), static_cast<long>(chunk));
            REQUIRE(buffer != nullptr);
            std::memcpy(buffer, bytes.data() + offset, chunk);
            REQUIRE(ogg_sync_wrote(&sync.handle(), static_cast<long>(chunk)) == 0);
            offset += chunk;
        }

        ogg_page page{};
        for (;;) {
            const int synced = ogg_sync_pageout(&sync.handle(), &page);
            // A negative value means the decoder could not frame the bytes at the
            // current offset. Freshly muxed output must never contain such a gap,
            // so a negative result is a hard error instead of an early exit.
            REQUIRE(synced >= 0);
            if (synced == 0) {
                break;
            }

            if (!stream.has_value()) {
                stream.emplace(ogg_page_serialno(&page));
            }
            result.page_numbers.push_back(ogg_page_pageno(&page));
            // ogg_page_bos/eos return the raw header bitmask (2 and 4), not a
            // boolean; normalize to 0/1 so the assertions below compare against
            // the documented boolean contract.
            result.page_bos.push_back(ogg_page_bos(&page) != 0);
            result.page_eos.push_back(ogg_page_eos(&page) != 0);
            result.page_granules.push_back(ogg_page_granulepos(&page));
            result.page_serials.push_back(ogg_page_serialno(&page));
            REQUIRE(ogg_stream_pagein(&stream->handle(), &page) == 0);

            ogg_packet packet{};
            for (;;) {
                const int extracted = ogg_stream_packetout(&stream->handle(), &packet);
                // Only 1 (a complete packet) and 0 (more data needed) are valid
                // for a stream this test just produced; a negative value is a
                // decoder hole and must be surfaced, not silently skipped.
                REQUIRE(extracted >= 0);
                if (extracted != 1) {
                    break;
                }
                std::vector<std::byte> data(static_cast<std::size_t>(packet.bytes));
                if (packet.bytes > 0) {
                    std::memcpy(data.data(), packet.packet, static_cast<std::size_t>(packet.bytes));
                }
                result.packets.push_back(std::move(data));
                result.packet_granules.push_back(packet.granulepos);
            }
        }

        return result;
    }

#ifdef _DEBUG
    // Rejects one exact library heap request so a framing failure path can be
    // exercised with the debug CRT, mirroring the existing zlib codec test hook.
    // The armed request is matched by the byte size libogg's framing source
    // computes, so nothing outside the intended allocation is disturbed.
    thread_local std::size_t rejected_size = 0;
    thread_local int rejected_type = 0;
    thread_local bool rejection_fired = false;

    int __cdecl allocation_hook(int alloc_type, void *, std::size_t size, int block_type, long, const unsigned char *,
                                int)
    {
        // Frees and CRT-internal bookkeeping blocks must always succeed; vetoing
        // either corrupts the debug heap. Only the single armed (size, type)
        // request is denied, and the hook allocates and logs nothing.
        if (alloc_type == _HOOK_FREE || block_type == _CRT_BLOCK) {
            return 1;
        }
        if (!rejection_fired && size == rejected_size && alloc_type == rejected_type) {
            rejection_fired = true;
            return 0;
        }
        return 1;
    }

    class scoped_allocation_failure final
    {
      public:
        scoped_allocation_failure(const std::size_t size, const int alloc_type)
            : previous_(_CrtSetAllocHook(allocation_hook))
        {
            rejected_size = size;
            rejected_type = alloc_type;
            rejection_fired = false;
        }

        ~scoped_allocation_failure()
        {
            rejected_size = 0;
            rejected_type = 0;
            static_cast<void>(_CrtSetAllocHook(previous_));
        }

        scoped_allocation_failure(const scoped_allocation_failure &) = delete;
        scoped_allocation_failure &operator=(const scoped_allocation_failure &) = delete;
        scoped_allocation_failure(scoped_allocation_failure &&) = delete;
        scoped_allocation_failure &operator=(scoped_allocation_failure &&) = delete;

        [[nodiscard]] bool fired() const noexcept
        {
            return rejection_fired;
        }

      private:
        _CRT_ALLOC_HOOK previous_ = nullptr;
    };
#endif
} // namespace

TEST_CASE("codecs: ogg muxes a single packet with a valid frame checksum")
{
    const auto payload = make_bytes(37, 0x1234u);
    // A first (beginning-of-stream) packet may only carry an unspecified (-1)
    // or zero granule position; the transport normalizes -1 to 0.
    const std::array packets{ogg_packet_view{payload, 0}};

    const auto encoded = mux(packets);
    require_valid_pages(encoded);

    const auto decoded = decode(encoded);
    REQUIRE(decoded.packets.size() == 1);
    REQUIRE(decoded.packets[0] == payload);
    REQUIRE(decoded.packet_granules == std::vector<std::int64_t>{0});
    REQUIRE(decoded.page_bos == std::vector<int>{1});
    REQUIRE(decoded.page_eos == std::vector<int>{1});
    REQUIRE(decoded.page_granules == std::vector<std::int64_t>{0});
    REQUIRE(decoded.page_numbers == std::vector<long>{0});
    REQUIRE(decoded.page_serials == std::vector<int>{0});
}

TEST_CASE("codecs: paged Ogg framing reads a long packet in bounded ranges", "[ogg-paged]")
{
    const auto payload = make_bytes(150000, 0x1234u);
    const std::array locations{unpackrat::codecs::ogg_packet_location{payload.size(), 0}};
    std::vector<std::byte> output;
    std::size_t largest_fetch = 0;
    unpackrat::codecs::write_ogg_paged(
        locations,
        [&](const std::size_t index, const std::uint64_t offset, const std::span<std::byte> destination) {
            REQUIRE(index == 0);
            REQUIRE(offset <= payload.size());
            REQUIRE(destination.size() <= payload.size() - offset);
            largest_fetch = (std::max)(largest_fetch, destination.size());
            REQUIRE(destination.size() <= 65025);
            std::copy_n(std::span<const std::byte>(payload).subspan(static_cast<std::size_t>(offset)).begin(),
                        destination.size(), destination.begin());
        },
        [&](const std::span<const std::byte> chunk) { output.insert(output.end(), chunk.begin(), chunk.end()); });
    REQUIRE(largest_fetch <= 65025);
    require_valid_pages(output);
    const auto decoded = decode(output);
    REQUIRE(decoded.packets == std::vector<std::vector<std::byte>>{payload});
    REQUIRE(decoded.packet_granules == std::vector<std::int64_t>{0});
    REQUIRE(decoded.page_bos.front() != 0);
    REQUIRE(decoded.page_eos.back() != 0);
}

TEST_CASE("codecs: paged Ogg keeps its fixed framing scratch off the callback stack", "[ogg-stack]")
{
    const auto payload = make_bytes(65026, 0x5251u);
    const std::array locations{unpackrat::codecs::ogg_packet_location{payload.size(), 0}};
    std::byte outer_marker{};
    const auto outer_address = reinterpret_cast<std::uintptr_t>(&outer_marker);
    std::uintptr_t farthest = 0;
    std::size_t written = 0;
    unpackrat::codecs::write_ogg_paged(
        locations,
        [&](const std::size_t, const std::uint64_t offset, const std::span<std::byte> destination) {
            std::byte inner_marker{};
            const auto inner_address = reinterpret_cast<std::uintptr_t>(&inner_marker);
            farthest = (std::max)(farthest, outer_address > inner_address ? outer_address - inner_address
                                                                          : inner_address - outer_address);
            std::copy_n(std::span<const std::byte>(payload).subspan(static_cast<std::size_t>(offset)).begin(),
                        destination.size(), destination.begin());
        },
        [&](const std::span<const std::byte> bytes) { written += bytes.size(); });
    INFO(farthest);
    CHECK(written > payload.size());
    CHECK(farthest < std::uintptr_t{20} * 1024);
}

TEST_CASE("codecs: paged Ogg framing keeps a sparse 257 MiB packet bounded", "[ogg-paged]")
{
    constexpr std::uint64_t payload_bytes = std::uint64_t{257} * 1024 * 1024 + 1;
    const std::array locations{unpackrat::codecs::ogg_packet_location{payload_bytes, 0}};
    std::uint64_t read_bytes = 0;
    std::uint64_t written_bytes = 0;
    std::size_t largest_read = 0;
    std::size_t largest_write = 0;
    unpackrat::codecs::write_ogg_paged(
        locations,
        [&](const std::size_t index, const std::uint64_t offset, const std::span<std::byte> destination) {
            REQUIRE(index == 0);
            REQUIRE(offset <= payload_bytes);
            REQUIRE(destination.size() <= payload_bytes - offset);
            largest_read = (std::max)(largest_read, destination.size());
            std::fill(destination.begin(), destination.end(), std::byte{0});
            read_bytes += destination.size();
        },
        [&](const std::span<const std::byte> chunk) {
            largest_write = (std::max)(largest_write, chunk.size());
            written_bytes += chunk.size();
        });
    REQUIRE(read_bytes == payload_bytes);
    REQUIRE(written_bytes > payload_bytes);
    REQUIRE(written_bytes < payload_bytes + std::uint64_t{2} * 1024 * 1024);
    REQUIRE(largest_read <= 65025);
    REQUIRE(largest_write <= 65025);
}

TEST_CASE("codecs: paged Ogg rejects invalid packet geometry before reading", "[ogg-paged]")
{
    using unpackrat::codecs::ogg_packet_location;
    std::size_t reads = 0;
    std::size_t writes = 0;
    const auto fetch = [&](std::size_t, std::uint64_t, std::span<std::byte>) { ++reads; };
    const auto sink = [&](std::span<const std::byte>) { ++writes; };
    const auto rejects = [&](const std::span<const ogg_packet_location> packets, const ogg_limits &limits = {}) {
        REQUIRE_THROWS_AS(unpackrat::codecs::write_ogg_paged(packets, fetch, sink, limits), ogg_error);
        REQUIRE(reads == 0);
        REQUIRE(writes == 0);
    };
    rejects({});
    const std::array first{ogg_packet_location{1, 0}};
    ogg_limits no_packets;
    no_packets.max_packets = 0;
    rejects(first, no_packets);
    const std::array positive_first{ogg_packet_location{1, 1}};
    rejects(positive_first);
    ogg_limits no_input;
    no_input.max_input_bytes = 0;
    rejects(first, no_input);
    const std::array illegal_negative{ogg_packet_location{1, 0}, ogg_packet_location{1, -2}};
    rejects(illegal_negative);
    const std::array backwards{ogg_packet_location{1, 0}, ogg_packet_location{1, 5}, ogg_packet_location{1, 4}};
    rejects(backwards);
}

TEST_CASE("codecs: paged Ogg zero-length packet and output limits preserve valid framing", "[ogg-paged]")
{
    using unpackrat::codecs::ogg_packet_location;
    const std::array empty{ogg_packet_location{0, -1}};
    std::vector<std::byte> output;
    unpackrat::codecs::write_ogg_paged(
        empty, [](std::size_t, std::uint64_t, std::span<std::byte>) { FAIL("empty packet requested input"); },
        [&](const std::span<const std::byte> chunk) { output.insert(output.end(), chunk.begin(), chunk.end()); });
    require_valid_pages(output);
    const auto decoded = decode(output);
    REQUIRE(decoded.packets == std::vector<std::vector<std::byte>>{std::vector<std::byte>{}});
    REQUIRE(decoded.packet_granules == std::vector<std::int64_t>{0});

    const std::array one_byte{ogg_packet_location{1, 0}};
    for (const std::size_t budget : {std::size_t{27}, std::size_t{28}}) {
        ogg_limits limits;
        limits.max_output_bytes = budget;
        std::size_t written = 0;
        REQUIRE_THROWS_AS(unpackrat::codecs::write_ogg_paged(
                              one_byte,
                              [](std::size_t, std::uint64_t, const std::span<std::byte> bytes) {
                                  std::fill(bytes.begin(), bytes.end(), std::byte{0});
                              },
                              [&](const std::span<const std::byte> bytes) { written += bytes.size(); }, limits),
                          ogg_error);
        REQUIRE(written == 0);
    }
}

TEST_CASE("codecs: ogg preserves packet order, sizes and granule positions")
{
    const auto first = make_bytes(300, 1);
    const auto second = make_bytes(1, 2);
    const auto third = make_bytes(70, 3);
    const std::array packets{
        ogg_packet_view{first, -1},
        ogg_packet_view{second, 5},
        ogg_packet_view{third, 9},
    };

    const auto encoded = mux(packets);
    require_valid_pages(encoded);

    const auto decoded = decode(encoded);
    REQUIRE(decoded.packets.size() == 3);
    REQUIRE(decoded.packets[0] == first);
    REQUIRE(decoded.packets[1] == second);
    REQUIRE(decoded.packets[2] == third);
    // The unknown (-1) first granule normalizes to the BOS value 0 while the
    // later known granules survive at the packet level.
    REQUIRE(decoded.packet_granules == std::vector<std::int64_t>{0, 5, 9});
    REQUIRE(std::count(decoded.page_bos.begin(), decoded.page_bos.end(), 1) == 1);
    REQUIRE(decoded.page_bos.front() == 1);
    REQUIRE(std::count(decoded.page_eos.begin(), decoded.page_eos.end(), 1) == 1);
    REQUIRE(decoded.page_eos.back() == 1);
    REQUIRE(decoded.page_granules.back() == 9);
    REQUIRE(std::is_sorted(decoded.page_numbers.begin(), decoded.page_numbers.end()));
    REQUIRE(std::all_of(decoded.page_serials.begin(), decoded.page_serials.end(),
                        [](const int serial) { return serial == 0; }));
}

TEST_CASE("codecs: ogg transports a zero-size packet")
{
    const std::array packets{ogg_packet_view{{}, 0}};

    const auto encoded = mux(packets);
    require_valid_pages(encoded);

    const auto decoded = decode(encoded);
    REQUIRE(decoded.packets.size() == 1);
    REQUIRE(decoded.packets[0].empty());
    REQUIRE(decoded.packet_granules == std::vector<std::int64_t>{0});
    REQUIRE(decoded.page_bos == std::vector<int>{1});
    REQUIRE(decoded.page_eos == std::vector<int>{1});
    REQUIRE(decoded.page_granules == std::vector<std::int64_t>{0});
}

TEST_CASE("codecs: ogg splits a large packet across several pages")
{
    const auto payload = make_bytes(70000, 7);
    // A first (BOS) packet may only carry zero or an unspecified granule. This
    // 70000-byte packet spans several pages, so the zero must land on the final
    // page instead of being lost to the multi-page split.
    const std::array packets{ogg_packet_view{payload, 0}};

    const auto encoded = mux(packets);
    require_valid_pages(encoded);

    const auto decoded = decode(encoded);
    REQUIRE(decoded.packets.size() == 1);
    REQUIRE(decoded.packets[0] == payload);
    REQUIRE(decoded.packet_granules == std::vector<std::int64_t>{0});
    REQUIRE(decoded.page_numbers.size() >= 2);
    REQUIRE(decoded.page_bos.front() == 1);
    REQUIRE(decoded.page_eos.back() == 1);
    REQUIRE(decoded.page_granules.front() == 0);
    REQUIRE(decoded.page_granules.back() == 0);
}

TEST_CASE("codecs: ogg marks unknown granule positions as -1")
{
    const auto payload = make_bytes(8, 23);
    const std::array packets{ogg_packet_view{payload, -1}, ogg_packet_view{payload, -1}};

    const auto decoded = decode(mux(packets));
    REQUIRE(decoded.packets.size() == 2);
    // Two pages in this fixture: the first carries the BOS packet whose granule
    // position is the initial 0, the second keeps the unknown -1 marker.
    REQUIRE(decoded.page_granules == std::vector<std::int64_t>{0, -1});
}

TEST_CASE("codecs: ogg rejects an empty packet list")
{
    const std::vector<ogg_packet_view> packets;
    REQUIRE_THROWS_AS(mux(packets), ogg_error);
}

TEST_CASE("codecs: ogg enforces the packet count budget")
{
    const auto payload = make_bytes(4, 9);
    const std::array packets{ogg_packet_view{payload, -1}, ogg_packet_view{payload, -1}};

    ogg_limits limits{};
    limits.max_packets = 1;
    REQUIRE_THROWS_AS(mux(packets, limits), ogg_error);
}

TEST_CASE("codecs: ogg enforces the input byte budget")
{
    const auto payload = make_bytes(64, 11);
    const std::array packets{ogg_packet_view{payload, -1}};

    ogg_limits limits{};
    limits.max_input_bytes = 63;
    REQUIRE_THROWS_AS(mux(packets, limits), ogg_error);
}

TEST_CASE("codecs: ogg enforces the output byte budget")
{
    const auto payload = make_bytes(64, 13);
    const std::array packets{ogg_packet_view{payload, -1}};

    ogg_limits header_exceeds{};
    header_exceeds.max_output_bytes = 10;
    REQUIRE_THROWS_AS(mux(packets, header_exceeds), ogg_error);

    ogg_limits body_exceeds{};
    body_exceeds.max_output_bytes = 30;
    REQUIRE_THROWS_AS(mux(packets, body_exceeds), ogg_error);
}

TEST_CASE("codecs: ogg accepts a small packet with an unrestricted input budget", "[ogg-large-limit]")
{
    const auto payload = make_bytes(4, 17);
    const std::array packets{ogg_packet_view{payload, -1}};

    ogg_limits limits{};
    limits.max_input_bytes = static_cast<std::size_t>(std::numeric_limits<long>::max()) + 1;
    REQUIRE(!mux(packets, limits).empty());
}

TEST_CASE("codecs: ogg rejects invalid and non-monotonic granule positions")
{
    const auto payload = make_bytes(4, 19);
    // A granule below -1 is not a valid marker at all.
    REQUIRE_THROWS_AS(mux(std::array{ogg_packet_view{payload, -2}}), ogg_error);
    // A decrease between known granules is rejected. The valid zero start keeps
    // this case from tripping the separate first-granule rule, so it exercises
    // monotonicity itself rather than the BOS constraint.
    REQUIRE_THROWS_AS(
        mux(std::array{ogg_packet_view{payload, 0}, ogg_packet_view{payload, 10}, ogg_packet_view{payload, 4}}),
        ogg_error);

    // -1 is an unknown marker and does not break monotonicity. A known granule
    // that opens the stream, a known granule after an unknown span, and the
    // unknown span itself must all survive with their exact packet positions.
    const auto first = make_bytes(4, 41);
    const auto second = make_bytes(5, 43);
    const auto third = make_bytes(6, 47);
    const auto fourth = make_bytes(7, 53);
    const std::array packets{
        ogg_packet_view{first, 0},
        ogg_packet_view{second, 10},
        ogg_packet_view{third, -1},
        ogg_packet_view{fourth, 12},
    };
    const auto decoded = decode(mux(packets));
    REQUIRE(decoded.packets.size() == 4);
    REQUIRE(decoded.packets[0] == first);
    REQUIRE(decoded.packets[1] == second);
    REQUIRE(decoded.packets[2] == third);
    REQUIRE(decoded.packets[3] == fourth);
    REQUIRE(decoded.packet_granules == std::vector<std::int64_t>{0, 10, -1, 12});
    REQUIRE(decoded.page_granules.back() == 12);
}

TEST_CASE("codecs: ogg rejects a nonzero first packet granule position")
{
    const auto payload = make_bytes(8, 19);
    // The first (BOS) packet of a logical stream may only carry an unknown (-1)
    // or zero granule position. Any other value is not a valid beginning-of-stream
    // marker, so the muxer must reject it instead of letting the transport
    // silently normalize it to the BOS value 0.
    const std::array packets{ogg_packet_view{payload, 1234}};
    REQUIRE_THROWS_AS(mux(packets), ogg_error);
}

TEST_CASE("codecs: ogg unwinds cleanly when the sink throws")
{
    const auto payload = make_bytes(32, 29);
    // The first packet must carry a valid zero granule so it passes validation
    // and reaches the sink, which is where this case expects the failure.
    const std::array packets{ogg_packet_view{payload, 0}};

    std::size_t calls = 0;
    REQUIRE_THROWS_AS(unpackrat::codecs::write_ogg(packets,
                                                   [&calls](std::span<const std::byte>) -> void {
                                                       ++calls;
                                                       throw std::runtime_error("sink rejected");
                                                   }),
                      std::runtime_error);
    REQUIRE(calls == 1);

    // No global state survives, so a later call yields a fresh, valid stream.
    const auto decoded = decode(mux(packets));
    REQUIRE(decoded.packets.size() == 1);
    REQUIRE(decoded.packets[0] == payload);
}

// Regression for the checksum helper's page framing. An Ogg page's body length is
// the sum of its segment-table lacing values, not the granule position field at
// header offset 6. This builds one known, correctly checksummed BOS page whose
// granule position (42) differs from its body size (5), so a helper that reads the
// wrong header field cannot accept it.
TEST_CASE("codecs: ogg checksum helper accepts a known valid page", "[test-safety]")
{
    const std::array<std::byte, 5> body{std::byte{'h'}, std::byte{'e'}, std::byte{'l'}, std::byte{'l'}, std::byte{'o'}};

    std::vector<std::byte> known(28 + body.size(), std::byte{0});
    const auto put = [&known](const std::size_t at, const std::size_t value) {
        known[at] = static_cast<std::byte>(value);
    };
    put(0, 'O');
    put(1, 'g');
    put(2, 'g');
    put(3, 'S');
    put(4, 0);            // stream structure version
    put(5, 0x02);         // beginning-of-stream flag
    put(6, 42);           // granule position; deliberately different from the body size
    put(26, 1);           // segment count
    put(27, body.size()); // lacing value == body length
    std::memcpy(known.data() + 28, body.data(), body.size());

    ogg_page page{};
    page.header = reinterpret_cast<unsigned char *>(known.data());
    page.header_len = 28;
    page.body = reinterpret_cast<unsigned char *>(known.data() + 28);
    page.body_len = static_cast<long>(body.size());
    ogg_page_checksum_set(&page);

    const auto expected_checksum = read_u32_le(reinterpret_cast<const unsigned char *>(known.data()) + 22);
    REQUIRE(expected_checksum != 0);
    require_valid_pages(std::span<const std::byte>(known));
}

#ifdef _DEBUG
TEST_CASE("codecs: ogg normalizes initialization failure")
{
    const auto payload = make_bytes(8, 31);
    const std::array packets{ogg_packet_view{payload, -1}};

    bool threw = false;
    bool rejected = false;
    {
        // libogg's stream init first allocates its initial 16 KiB body buffer.
        const scoped_allocation_failure failure(16u * 1024u, _HOOK_ALLOC);
        try {
            static_cast<void>(mux(packets));
        } catch (const ogg_error &) {
            threw = true;
        }
        rejected = failure.fired();
    }
    REQUIRE(rejected);
    REQUIRE(threw);
}

TEST_CASE("codecs: ogg normalizes a failed packet queue allocation")
{
    // The first packet exceeds libogg's initial 16 KiB body buffer, so queueing it
    // grows that buffer to 16384 + 20000 + 1024 bytes, exactly as libogg's framing
    // source computes; rejecting that realloc must surface as an ogg_error.
    const auto payload = make_bytes(20000, 33);
    const std::array packets{ogg_packet_view{payload, -1}};

    bool threw = false;
    bool rejected = false;
    {
        const scoped_allocation_failure failure(16u * 1024u + 20000u + 1024u, _HOOK_REALLOC);
        try {
            static_cast<void>(mux(packets));
        } catch (const ogg_error &) {
            threw = true;
        }
        rejected = failure.fired();
    }
    REQUIRE(rejected);
    REQUIRE(threw);
}

TEST_CASE("codecs: ogg allocator hook never vetoes a FREE or CRT request", "[test-safety]")
{
    // Direct, non-mutating probe of the debug CRT allocation hook. A FREE request
    // and a CRT-internal bookkeeping block must always be permitted, because
    // vetoing either corrupts the debug heap, even while the hook is armed to
    // reject a normal-heap request of the same size. The hook function is invoked
    // directly, so no real allocation or deallocation happens.
    int free_result = 0;
    int crt_result = 0;
    {
        const scoped_allocation_failure armed(16u * 1024u, _HOOK_ALLOC);
        free_result = allocation_hook(_HOOK_FREE, nullptr, 0, _FREE_BLOCK, 0, nullptr, 0);
        crt_result = allocation_hook(_HOOK_ALLOC, nullptr, 16u * 1024u, _CRT_BLOCK, 0, nullptr, 0);
    }
    REQUIRE(free_result == 1);
    REQUIRE(crt_result == 1);
}
#endif
