#include "../../core/godot/key_recovery.h"
#include "../support/godot_key_fixtures.h"
#include <algorithm>
#include <catch2/catch_test_macros.hpp>
#include <limits>
#include <sstream>
namespace
{
    std::string as_string(const std::vector<std::byte> &bytes)
    {
        return {reinterpret_cast<const char *>(bytes.data()), bytes.size()};
    }
} // namespace
TEST_CASE("godot key recovery: recognizes bounded scalar and SIMD constant patterns", "[godot][key-recovery]")
{
    using test::support::key_shape;
    for (const auto shape : {key_shape::direct_scalar, key_shape::pointer_scalar, key_shape::direct_simd,
                             key_shape::pointer_simd, key_shape::xor3_scalar, key_shape::xor3_simd}) {
        for (bool wide : {false, true}) {
            CAPTURE(static_cast<unsigned>(shape), wide);
            const auto fixture = test::support::make_key_fixture(shape, wide);
            std::istringstream stream(as_string(fixture.image));
            unpackrat::godot::key_recovery_budget budget;
            const auto keys = unpackrat::godot::recover_key_candidates(stream, budget);
            REQUIRE(std::ranges::find(keys, fixture.expected) != keys.end());
            CHECK(budget.candidates == keys.size());
            CHECK(budget.instructions <= unpackrat::godot::key_recovery_limits{}.max_instructions);
        }
    }
}
TEST_CASE("godot key recovery: ordinary data and unanchored code never yield candidates", "[godot][key-recovery]")
{
    std::istringstream stream("ordinary file");
    unpackrat::godot::key_recovery_budget budget;
    CHECK(unpackrat::godot::recover_key_candidates(stream, budget).empty());
    auto fixture = test::support::make_key_fixture();
    fixture.image[0x201] = std::byte{0};
    std::istringstream unanchored(as_string(fixture.image));
    CHECK(unpackrat::godot::recover_key_candidates(unanchored, budget).empty());
}
TEST_CASE("godot key recovery: aggregate limits fail closed", "[godot][key-recovery]")
{
    const auto fixture = test::support::make_key_fixture();
    for (int kind = 0; kind < 4; ++kind) {
        CAPTURE(kind);
        unpackrat::godot::key_recovery_limits limits;
        if (kind == 0)
            limits.max_scanned_bytes = 0;
        if (kind == 1)
            limits.max_instructions = 0;
        if (kind == 2)
            limits.max_candidates = 0;
        if (kind == 3)
            limits.max_pattern_instructions = 0;
        unpackrat::godot::key_recovery_budget budget;
        std::istringstream stream(as_string(fixture.image));
        CHECK(unpackrat::godot::recover_key_candidates(stream, budget, limits).empty());
    }
}

TEST_CASE("godot key recovery: a large pack reader still exposes its bounded key consumer",
          "[godot][key-recovery][key-consumer-window]")
{
    auto fixture = test::support::make_key_fixture();
    std::copy_n(fixture.image.begin() + 0x20a, 11, fixture.image.begin() + 0x40a);
    std::fill(fixture.image.begin() + 0x20a, fixture.image.begin() + 0x40a, std::byte{0x90});
    // Relocate the RIP-relative LEA along with its instruction.
    fixture.image[0x40d] = std::byte{0xef};
    fixture.image[0x40e] = std::byte{0x0d};
    std::istringstream stream(as_string(fixture.image));
    unpackrat::godot::key_recovery_budget budget;
    const auto keys = unpackrat::godot::recover_key_candidates(stream, budget);
    CHECK(std::ranges::find(keys, fixture.expected) != keys.end());
}

TEST_CASE("godot key recovery: candidate material has a fixed upper bound even for relaxed caller limits",
          "[godot][key-recovery][key-material-bound]")
{
    auto fixture = test::support::make_key_fixture();
    fixture.image.resize(0x2400);
    const auto put32 = [&](std::size_t at, std::uint32_t value) {
        for (std::size_t i = 0; i < 4; ++i)
            fixture.image[at + i] = static_cast<std::byte>(value >> (8 * i));
    };
    put32(0x1b0 + 8, 0x1200);
    put32(0x1b0 + 16, 0x1200);
    std::size_t cursor = 0x20a;
    for (std::size_t index = 0; index < 129; ++index) {
        fixture.image[cursor++] = std::byte{0x48};
        fixture.image[cursor++] = std::byte{0x8d};
        fixture.image[cursor++] = std::byte{0x0d};
        const auto target = std::uint64_t{0x140002000} + static_cast<std::uint64_t>(index) * 32;
        const auto next = std::uint64_t{0x140001000} + cursor - 0x200 + 4;
        put32(cursor, static_cast<std::uint32_t>(target - next));
        cursor += 4;
        std::fill_n(fixture.image.begin() +
                        static_cast<std::ptrdiff_t>(0x1200 + static_cast<std::uint64_t>(index) * 32),
                    32, static_cast<std::byte>(index + 1));
    }
    fixture.image[cursor] = std::byte{0xc3};
    unpackrat::godot::key_recovery_limits limits;
    limits.max_candidates = std::numeric_limits<std::uint64_t>::max();
    unpackrat::godot::key_recovery_budget budget;
    std::istringstream stream(as_string(fixture.image));
    const auto keys = unpackrat::godot::recover_key_candidates(stream, budget, limits);
    REQUIRE(keys.size() == 128);
    CHECK(budget.candidates == 128);
    CHECK(keys.front() != keys.back());
}

namespace
{
    using unpackrat::godot::key_recovery_budget;
    using unpackrat::godot::key_recovery_error;
    using unpackrat::godot::key_recovery_limits;
    void patch(test::support::key_fixture &fixture, std::size_t offset, std::uint64_t value, std::size_t width = 4)
    {
        for (std::size_t i = 0; i < width; ++i)
            fixture.image[offset + i] = static_cast<std::byte>(value >> (8 * i));
    }
    auto recover(const test::support::key_fixture &fixture, const key_recovery_limits &limits = {})
    {
        std::istringstream stream(as_string(fixture.image));
        key_recovery_budget budget;
        return unpackrat::godot::recover_key_candidates(stream, budget, limits);
    }
} // namespace
TEST_CASE("godot key recovery: malformed PE ranges and modes are rejected before scanning", "[godot][key-recovery]")
{
    struct mutation
    {
        std::uint64_t value;
        std::size_t offset;
        std::size_t width;
        constexpr mutation(std::size_t changed_offset, std::uint64_t changed_value, std::size_t changed_width)
            : value(changed_value), offset(changed_offset), width(changed_width)
        {
        }
    };
    const mutation cases[] = {{0x80, 0, 4},
                              {0x84, 0x9999, 2},
                              {0x86, 0, 2},
                              {0x86, 97, 2},
                              {0x94, 63, 2},
                              {0x94, 4097, 2},
                              {0x98, 0x10b, 2},
                              {0xb0, std::numeric_limits<std::uint64_t>::max() - 0x800, 8},
                              {0xb0, std::numeric_limits<std::uint64_t>::max() - 0x1800, 8},
                              {0x188 + 20, 0x2000, 4},
                              {0x188 + 16, 0x2000, 4},
                              {0x1b0 + 12, 0x1000, 4},
                              {0x3c, 0x1601, 4},
                              {0x3c, 0x15f8, 4}};
    for (const auto &row : cases) {
        CAPTURE(row.offset, row.value);
        auto fixture = test::support::make_key_fixture();
        patch(fixture, row.offset, row.value, row.width);
        CHECK_THROWS_AS(recover(fixture), key_recovery_error);
    }
    auto ordinary = test::support::make_key_fixture();
    ordinary.image[0] = std::byte{0};
    CHECK(recover(ordinary).empty());
    std::istringstream failed("data");
    failed.setstate(std::ios::badbit);
    key_recovery_budget budget;
    CHECK_THROWS_AS(unpackrat::godot::recover_key_candidates(failed, budget), key_recovery_error);
}
TEST_CASE("godot key recovery: section order and empty sections have explicit range semantics", "[godot][key-recovery]")
{
    auto reversed = test::support::make_key_fixture();
    for (std::size_t i = 0; i < 40; ++i)
        std::swap(reversed.image[0x188 + i], reversed.image[0x1b0 + i]);
    const auto keys = recover(reversed);
    CHECK(std::ranges::find(keys, reversed.expected) != keys.end());
    for (const std::size_t row : {0x188U, 0x1b0U}) {
        auto empty = test::support::make_key_fixture();
        patch(empty, row + 8, 0);
        patch(empty, row + 16, 0);
        CHECK(recover(empty).empty());
    }
    auto short_target = test::support::make_key_fixture(test::support::key_shape::pointer_scalar);
    patch(short_target, 0x1300, 0x1400023f0, 8);
    const auto absent = recover(short_target);
    CHECK(std::ranges::find(absent, short_target.expected) == absent.end());
}
TEST_CASE("godot key recovery: instruction boundaries and consumer windows fail closed", "[godot][key-recovery]")
{
    SECTION("an anchor after a prefix is decoded from a bounded lookback")
    {
        auto fixture = test::support::make_key_fixture();
        std::copy_n(fixture.image.begin() + 0x200, 21, fixture.image.begin() + 0x240);
        std::fill(fixture.image.begin() + 0x200, fixture.image.begin() + 0x240, std::byte{0xcc});
        patch(fixture, 0x24d, 0xfaf);
        const auto keys = recover(fixture);
        CHECK(std::ranges::find(keys, fixture.expected) != keys.end());
    }
    SECTION("adjacent pack literals cannot validate the wrong instruction")
    {
        auto fixture = test::support::make_key_fixture();
        std::copy_n(fixture.image.begin() + 0x200, 21, fixture.image.begin() + 0x205);
        patch(fixture, 0x212, 0xfea);
        const auto keys = recover(fixture);
        CHECK(std::ranges::find(keys, fixture.expected) != keys.end());
    }
    SECTION("truncated instructions end a recognized window")
    {
        auto fixture = test::support::make_key_fixture();
        patch(fixture, 0x188 + 16, 11);
        patch(fixture, 0x188 + 8, 11);
        fixture.image[0x20a] = std::byte{0x0f};
        CHECK(recover(fixture).empty());
    }
    SECTION("a window can end at the last complete instruction")
    {
        auto fixture = test::support::make_key_fixture();
        patch(fixture, 0x188 + 16, 17);
        patch(fixture, 0x188 + 8, 17);
        const auto keys = recover(fixture);
        CHECK(std::ranges::find(keys, fixture.expected) != keys.end());
    }
    SECTION("a deliberately narrow instruction allowance cannot reach a key")
    {
        key_recovery_limits limits;
        limits.max_pattern_instructions = 1;
        CHECK(recover(test::support::make_key_fixture(), limits).empty());
    }
    SECTION("calls outside backed executable sections are not followed")
    {
        auto fixture = test::support::make_key_fixture(test::support::key_shape::xor3_simd);
        patch(fixture, 0x20b, 0x7fffffff);
        CHECK(recover(fixture).empty());
    }
}
TEST_CASE("godot key recovery: a three-array formula requires every syntactic constant", "[godot][key-recovery]")
{
    for (int change = 0; change < 3; ++change) {
        auto fixture = test::support::make_key_fixture(test::support::key_shape::xor3_scalar);
        if (change == 0) {
            fixture.image[0xe2b] = std::byte{0x90};
            fixture.image[0xe2c] = std::byte{0x90};
        }
        if (change == 1)
            patch(fixture, 0xe27, 0x7fffffff);
        if (change == 2)
            patch(fixture, 0xe27, 0xffffffff);
        CAPTURE(change);
        const auto keys = recover(fixture);
        CHECK(std::ranges::find(keys, fixture.expected) == keys.end());
    }
}
TEST_CASE("godot key recovery: initializer searches share byte and instruction allowances", "[godot][key-recovery]")
{
    auto fixture = test::support::make_key_fixture(test::support::key_shape::xor3_scalar);
    SECTION("a second scan cannot exceed the byte allowance")
    {
        key_recovery_limits limits;
        limits.max_scanned_bytes = 4504;
        CHECK(recover(fixture, limits).empty());
    }
    SECTION("an already spent allowance cannot wrap")
    {
        key_recovery_limits limits;
        key_recovery_budget budget;
        budget.instructions = limits.max_instructions + 1;
        std::istringstream stream(as_string(fixture.image));
        CHECK(unpackrat::godot::recover_key_candidates(stream, budget, limits).empty());
        CHECK(budget.instructions == limits.max_instructions + 1);
    }
    SECTION("malformed address-reference prefixes are ignored in the initializer pass")
    {
        fixture.image[0x11ff] = std::byte{0x48};
        fixture.image[0x1100] = std::byte{0x8b};
        fixture.image[0x1101] = std::byte{0xc0};
        const auto keys = recover(fixture);
        CHECK(std::ranges::find(keys, fixture.expected) != keys.end());
    }
}

TEST_CASE("godot key recovery: unbacked initializer targets are retained under a fixed bound", "[godot][key-recovery]")
{
    auto fixture = test::support::make_key_fixture();
    patch(fixture, 0x1b0 + 8, 0x1000);
    std::size_t cursor = 0x20a;
    for (std::size_t i = 0; i < 33; ++i) {
        fixture.image[cursor++] = std::byte{0x48};
        fixture.image[cursor++] = std::byte{0x8d};
        fixture.image[cursor++] = std::byte{0x0d};
        const auto next = std::uint64_t{0x140001000} + cursor - 0x200 + 4;
        patch(fixture, cursor, static_cast<std::uint32_t>(0x140002600ULL + static_cast<std::uint64_t>(i) * 32 - next));
        cursor += 4;
    }
    fixture.image[cursor] = std::byte{0xc3};
    CHECK(recover(fixture).empty());
}
