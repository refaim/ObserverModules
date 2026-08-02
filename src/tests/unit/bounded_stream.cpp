#include "../../core/io/bounded_stream.h"

#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
#include <limits>
#include <sstream>
#include <streambuf>
#include <string>
#include <string_view>

#include <catch2/catch_test_macros.hpp>

namespace
{
    class controlled_stream_buffer final : public std::streambuf
    {
      public:
        explicit controlled_stream_buffer(const std::string_view contents, const std::streamoff position = 0)
            : contents_(contents), position_(position)
        {
        }

        void fail_seeks(const bool value) noexcept
        {
            fail_seeks_ = value;
        }

        void limit_reads(const bool value) noexcept
        {
            limit_reads_ = value;
        }

        void set_position(const std::streamoff value) noexcept
        {
            position_ = value;
        }

      protected:
        pos_type seekoff(const off_type offset, const std::ios_base::seekdir direction,
                         const std::ios_base::openmode mode) override
        {
            if (fail_seeks_ || (mode & std::ios_base::in) == 0) {
                return pos_type{off_type{-1}};
            }

            off_type base = 0;
            if (direction == std::ios_base::cur) {
                base = position_;
            } else if (direction == std::ios_base::end) {
                base = static_cast<off_type>(contents_.size());
            }
            const auto result = base + offset;
            if (result < 0) {
                return pos_type{off_type{-1}};
            }
            position_ = result;
            return pos_type{position_};
        }

        pos_type seekpos(const pos_type position, const std::ios_base::openmode mode) override
        {
            if (fail_seeks_ || (mode & std::ios_base::in) == 0 || position < 0) {
                return pos_type{off_type{-1}};
            }
            position_ = static_cast<off_type>(position);
            return pos_type{position_};
        }

        std::streamsize xsgetn(char_type *destination, const std::streamsize count) override
        {
            if (count <= 0 || position_ < 0 || position_ >= static_cast<off_type>(contents_.size())) {
                return 0;
            }
            const auto available = static_cast<std::streamsize>(contents_.size() - static_cast<std::size_t>(position_));
            auto copied = std::min(count, available);
            if (limit_reads_) {
                copied = std::min<std::streamsize>(copied, 1);
            }
            std::memcpy(destination, contents_.data() + position_, static_cast<std::size_t>(copied));
            position_ += copied;
            return copied;
        }

      private:
        std::string contents_;
        std::streamoff position_ = 0;
        bool fail_seeks_ = false;
        bool limit_reads_ = false;
    };
} // namespace

TEST_CASE("bounded stream: reads and rejects out-of-range operations")
{
    std::istringstream source("abcd");
    observer::io::bounded_stream input(source);
    REQUIRE(input.size() == 4);
    REQUIRE(input.position() == 0);

    input.seek_absolute(1);
    REQUIRE(input.remaining() == 3);
    std::array<char, 2> value{};
    input.read_exact(value.data(), value.size());
    REQUIRE(value == std::array{'b', 'c'});
    REQUIRE(input.remaining() == 1);

    REQUIRE_THROWS_AS(input.read_exact(value.data(), value.size()), observer::io::read_error);
    REQUIRE_THROWS_AS(input.seek_absolute(-1), observer::io::read_error);
    REQUIRE_THROWS_AS(input.seek_absolute(5), observer::io::read_error);
}

TEST_CASE("bounded stream: reads trivial values")
{
    std::istringstream source(std::string{"\x78\x56\x34\x12", 4});
    observer::io::bounded_stream input(source);
    REQUIRE(input.read_trivial<std::uint32_t>() == 0x12345678);
}

TEST_CASE("bounded stream: normalizes stream positioning failures")
{
    SECTION("invalid initial position")
    {
        controlled_stream_buffer buffer("abc", -1);
        std::istream source(&buffer);
        REQUIRE_THROWS_AS(observer::io::bounded_stream(source), observer::io::read_error);
    }

    SECTION("end precedes initial position")
    {
        controlled_stream_buffer buffer("", 1);
        std::istream source(&buffer);
        REQUIRE_THROWS_AS(observer::io::bounded_stream(source), observer::io::read_error);
    }

    SECTION("constructor receives an exception")
    {
        controlled_stream_buffer buffer("abc");
        buffer.fail_seeks(true);
        std::istream source(&buffer);
        source.exceptions(std::ios_base::failbit);
        REQUIRE_THROWS_AS(observer::io::bounded_stream(source), observer::io::read_error);
    }

    SECTION("position is negative")
    {
        std::istringstream source("abc");
        observer::io::bounded_stream input(source);
        source.setstate(std::ios_base::failbit);
        REQUIRE_THROWS_AS(input.position(), observer::io::read_error);
    }

    SECTION("position exceeds the captured size")
    {
        controlled_stream_buffer buffer("abc");
        std::istream source(&buffer);
        observer::io::bounded_stream input(source);
        buffer.set_position(4);
        REQUIRE_THROWS_AS(input.position(), observer::io::read_error);
    }

    SECTION("position receives an exception")
    {
        std::istringstream source("abc");
        observer::io::bounded_stream input(source);
        source.exceptions(std::ios_base::failbit);
        REQUIRE_THROWS_AS(source.setstate(std::ios_base::failbit), std::ios_base::failure);
        REQUIRE_THROWS_AS(input.position(), observer::io::read_error);
    }
}

TEST_CASE("bounded stream: normalizes seek and read failures")
{
    SECTION("seek reports a failed stream state")
    {
        controlled_stream_buffer buffer("abc");
        std::istream source(&buffer);
        observer::io::bounded_stream input(source);
        buffer.fail_seeks(true);
        REQUIRE_THROWS_AS(input.seek_absolute(0), observer::io::read_error);
    }

    SECTION("seek throws")
    {
        controlled_stream_buffer buffer("abc");
        std::istream source(&buffer);
        observer::io::bounded_stream input(source);
        buffer.fail_seeks(true);
        source.exceptions(std::ios_base::failbit);
        REQUIRE_THROWS_AS(input.seek_absolute(0), observer::io::read_error);
    }

    SECTION("read is shorter than the captured extent")
    {
        controlled_stream_buffer buffer("abc");
        std::istream source(&buffer);
        observer::io::bounded_stream input(source);
        buffer.limit_reads(true);
        std::array<char, 2> output{};
        REQUIRE_THROWS_AS(input.read_exact(output.data(), output.size()), observer::io::read_error);
    }

    SECTION("read throws")
    {
        controlled_stream_buffer buffer("abc");
        std::istream source(&buffer);
        observer::io::bounded_stream input(source);
        buffer.limit_reads(true);
        source.exceptions(std::ios_base::failbit);
        std::array<char, 2> output{};
        REQUIRE_THROWS_AS(input.read_exact(output.data(), output.size()), observer::io::read_error);
    }

    SECTION("requested size cannot be represented by the stream API")
    {
        std::istringstream source("abc");
        observer::io::bounded_stream input(source);
        if constexpr (std::numeric_limits<std::size_t>::max() >
                      static_cast<std::uintmax_t>(std::numeric_limits<std::streamsize>::max())) {
            constexpr auto impossible_size = static_cast<std::size_t>(std::numeric_limits<std::streamsize>::max()) + 1;
            REQUIRE_THROWS_AS(input.read_exact(nullptr, impossible_size), observer::io::read_error);
        } else {
            SUCCEED("size_t cannot represent a request larger than streamsize on this ABI");
        }
    }
}
