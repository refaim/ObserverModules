#include "ogg_codec.h"

#include <array>
#include <limits>
#include <memory>

#include <ogg/ogg.h>

namespace unpackrat::codecs
{
    void check_ogg_packet_size(const std::uint64_t bytes)
    {
        if (bytes > static_cast<std::uint64_t>((std::numeric_limits<long>::max)())) {
            throw ogg_error("Ogg packet exceeds the transport packet size range");
        }
    }

    namespace
    {
        // A fixed serial number keeps the produced bitstream deterministic.
        constexpr int ogg_serial_number = 0;

        // libogg reads packet bodies through a `long` length and expects a valid
        // pointer even for an empty packet, so empty packets borrow this byte.
        constexpr std::array<unsigned char, 1> empty_packet_byte{0};

        using sink_type = std::function<void(std::span<const std::byte>)>;

        // Owns the framing state and always runs the matching library release,
        // including while an exception unwinds out of a sink call.
        class stream_state final
        {
          public:
            explicit stream_state(const int serial_number)
            {
                if (ogg_stream_init(&state_, serial_number) != 0) {
                    // ogg_stream_init releases its own partial state on failure,
                    // so ownership is only claimed once initialization succeeds. A
                    // throwing constructor never runs the destructor, so a state
                    // that survives construction always needs the matching clear.
                    throw ogg_error("Failed to initialize the Ogg stream state");
                }
            }

            ~stream_state()
            {
                static_cast<void>(ogg_stream_clear(&state_));
            }

            stream_state(const stream_state &) = delete;
            stream_state &operator=(const stream_state &) = delete;
            stream_state(stream_state &&) = delete;
            stream_state &operator=(stream_state &&) = delete;

            [[nodiscard]] ogg_stream_state &handle() noexcept
            {
                return state_;
            }

          private:
            ogg_stream_state state_{};
        };

        [[nodiscard]] std::span<const std::byte> as_bytes(const unsigned char *data, const long size) noexcept
        {
            return {reinterpret_cast<const std::byte *>(data), static_cast<std::size_t>(size)};
        }

        // Every budget and invariant that the caller controls is checked before
        // the framing library is touched.
        void validate(const std::span<const ogg_packet_view> packets, const ogg_limits &limits)
        {
            if (packets.empty()) {
                throw ogg_error("Ogg muxing requires at least one packet");
            }
            if (packets.size() > limits.max_packets) {
                throw ogg_error("Ogg packet count exceeds the configured budget");
            }
            // The first packet opens the logical stream, so its granule position is
            // the transport's beginning-of-stream marker: only an unspecified (-1)
            // or zero value is valid. A positive first value is rejected here,
            // before the framing library is invoked or any page reaches the sink.
            if (packets.front().granule_position > 0) {
                throw ogg_error("Ogg first packet granule position must be -1 or 0");
            }

            std::size_t total_input = 0;
            std::int64_t previous_known_granule = -1;
            for (const auto &packet : packets) {
                // Ogg framing stores each packet length in a signed long. A caller's generous
                // aggregate budget is fine; only the packet actually passed to libogg must fit.
                check_ogg_packet_size(packet.data.size());
                if (packet.data.size() > limits.max_input_bytes - total_input) {
                    throw ogg_error("Ogg input exceeds the configured byte budget");
                }
                total_input += packet.data.size();

                if (packet.granule_position < -1) {
                    throw ogg_error("Ogg packet granule position must be -1 or non-negative");
                }
                if (packet.granule_position >= 0) {
                    if (previous_known_granule >= 0 && packet.granule_position < previous_known_granule) {
                        throw ogg_error("Ogg packet granule positions must be monotonic");
                    }
                    previous_known_granule = packet.granule_position;
                }
            }
        }

        void emit_page(const ogg_page &page, const sink_type &sink, std::size_t &written,
                       const std::size_t max_output_bytes)
        {
            const auto header_size = static_cast<std::size_t>(page.header_len);
            const auto body_size = static_cast<std::size_t>(page.body_len);
            if (header_size > max_output_bytes - written || body_size > max_output_bytes - written - header_size) {
                throw ogg_error("Ogg output exceeds the configured byte budget");
            }
            written += header_size + body_size;
            sink(as_bytes(page.header, page.header_len));
            if (body_size != 0) {
                sink(as_bytes(page.body, page.body_len));
            }
        }
    } // namespace

    void write_ogg(const std::span<const ogg_packet_view> packets, const sink_type &sink, const ogg_limits &limits)
    {
        validate(packets, limits);

        stream_state state(ogg_serial_number);
        std::size_t written = 0;
        const auto last_index = packets.size() - 1;

        for (std::size_t index = 0; index < packets.size(); ++index) {
            const auto &view = packets[index];
            const auto *raw = view.data.empty() ? empty_packet_byte.data()
                                                : reinterpret_cast<const unsigned char *>(view.data.data());

            // The first packet opens the logical stream. validate already rejected
            // any positive first value, so an unspecified (-1) position becomes the
            // transport's BOS value 0 while every later packet keeps its own marker.
            std::int64_t granule = view.granule_position;
            if (index == 0 && granule < 0) {
                granule = 0;
            }

            ogg_packet packet{};
            packet.packet = const_cast<unsigned char *>(raw);
            packet.bytes = static_cast<long>(view.data.size());
            packet.b_o_s = index == 0 ? 1 : 0;
            packet.e_o_s = index == last_index ? 1 : 0;
            packet.granulepos = granule;
            packet.packetno = static_cast<ogg_int64_t>(index);

            if (ogg_stream_packetin(&state.handle(), &packet) != 0) {
                throw ogg_error("Failed to queue an Ogg packet");
            }

            // A page records its granule only on the last packet it carries, so a
            // known granule survives the round trip only when its packet ends a
            // page. Force a flush after every known packet so a later packet cannot
            // coalesce over it. Unknown (-1) packets may share a page, and pageout
            // drains the final unknown packet because e_o_s forces the page. Every
            // successful drain retires at least one lacing entry, so each loop makes
            // progress and terminates without an unbounded flush.
            ogg_page page{};
            if (granule >= 0) {
                while (ogg_stream_flush(&state.handle(), &page) != 0) {
                    emit_page(page, sink, written, limits.max_output_bytes);
                }
            } else {
                while (ogg_stream_pageout(&state.handle(), &page) != 0) {
                    emit_page(page, sink, written, limits.max_output_bytes);
                }
            }
        }
    }

    void write_ogg_paged(const std::span<const ogg_packet_location> packets,
                         const std::function<void(std::size_t, std::uint64_t, std::span<std::byte>)> &fetch,
                         const sink_type &sink, const ogg_limits &limits)
    {
        if (packets.empty() || packets.size() > limits.max_packets || packets.front().granule_position > 0) {
            throw ogg_error("Ogg packet sequence has invalid geometry");
        }
        std::size_t total_input = 0;
        std::int64_t previous_known = -1;
        for (const auto &packet : packets) {
            if (packet.length > limits.max_input_bytes - total_input) {
                throw ogg_error("Ogg input exceeds the configured byte budget");
            }
            total_input += static_cast<std::size_t>(packet.length);
            if (packet.granule_position < -1 ||
                (packet.granule_position >= 0 && previous_known >= 0 && packet.granule_position < previous_known)) {
                throw ogg_error("Ogg packet granules are invalid");
            }
            if (packet.granule_position >= 0) {
                previous_known = packet.granule_position;
            }
        }

        // A page has at most 255 lacing entries, each funding at most 255 bytes. libogg exposes
        // its checksum function independently of packetin, so no complete packet is materialized.
        std::array<unsigned char, 282> header{};
        auto body_storage = std::make_unique<std::array<unsigned char, 65025>>();
        auto &body = *body_storage;
        std::size_t written = 0;
        std::uint64_t page_number = 0;
        for (std::size_t index = 0; index < packets.size(); ++index) {
            const auto &packet = packets[index];
            std::uint64_t remaining = packet.length;
            std::uint64_t packet_offset = 0;
            bool first_page = true;
            bool packet_complete = false;
            while (!packet_complete) {
                header.fill(0);
                header[0] = 'O';
                header[1] = 'g';
                header[2] = 'g';
                header[3] = 'S';
                header[5] = static_cast<unsigned char>((first_page ? 0 : 1) | (page_number == 0 ? 2 : 0));
                std::size_t segments = 0;
                std::size_t body_size = 0;
                while (segments < 255) {
                    const auto take = static_cast<unsigned char>((std::min)(remaining, std::uint64_t{255}));
                    header[27 + segments] = take;
                    ++segments;
                    body_size += take;
                    remaining -= take;
                    if (take < 255) {
                        packet_complete = true;
                        break;
                    }
                }
                if (packet_complete && index + 1 == packets.size()) {
                    header[5] |= 4;
                }
                const std::int64_t granule =
                    packet_complete ? (index == 0 && packet.granule_position < 0 ? 0 : packet.granule_position) : -1;
                const auto put_le = [&](const std::size_t start, const std::uint64_t value, const std::size_t count) {
                    for (std::size_t byte = 0; byte < count; ++byte) {
                        header[start + byte] = static_cast<unsigned char>(value >> (8 * byte));
                    }
                };
                put_le(6, static_cast<std::uint64_t>(granule), 8);
                // The zero-initialized serial number and checksum are deterministic.
                // The sequence field is four bytes on the wire. libogg's reader accepts its
                // natural modulo-2^32 wrap, while the wider counter keeps BOS tied to page zero.
                put_le(18, page_number, 4);
                put_le(26, segments, 1);

                if (body_size != 0) {
                    fetch(index, packet_offset,
                          std::span<std::byte>(reinterpret_cast<std::byte *>(body.data()), body_size));
                }
                packet_offset += body_size;
                ogg_page page{};
                page.header = header.data();
                page.header_len = 27L + static_cast<long>(segments);
                page.body = body.data();
                page.body_len = static_cast<long>(body_size);
                ogg_page_checksum_set(&page);
                const std::size_t header_size = 27 + segments;
                if (header_size > limits.max_output_bytes - written ||
                    body_size > limits.max_output_bytes - written - header_size) {
                    throw ogg_error("Ogg output exceeds the configured byte budget");
                }
                sink(as_bytes(page.header, page.header_len));
                if (body_size != 0) {
                    sink(as_bytes(page.body, page.body_len));
                }
                written += header_size + body_size;
                ++page_number;
                first_page = false;
            }
        }
    }
} // namespace unpackrat::codecs
