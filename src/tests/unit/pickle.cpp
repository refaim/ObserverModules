#include "../../modules/renpy/pickle.h"
#include "../../archive.h"

#include <array>
#include <bit>
#include <cstddef>
#include <cstdint>
#include <span>
#include <string>
#include <vector>

#include <catch2/catch_test_macros.hpp>

namespace
{
    template <std::size_t Size> pickle::value_ptr load(const std::array<std::uint8_t, Size> &input)
    {
        return pickle::loads(std::span{
            reinterpret_cast<const std::byte *>(input.data()),
            input.size(),
        });
    }

    pickle::value_ptr load(const std::initializer_list<std::uint8_t> input)
    {
        const std::vector<std::uint8_t> bytes(input);
        return pickle::loads(std::span{
            reinterpret_cast<const std::byte *>(bytes.data()),
            bytes.size(),
        });
    }

    pickle::value_ptr load_memo_copy(const std::initializer_list<std::uint8_t> encoded_value)
    {
        std::vector<std::uint8_t> bytes{']'};
        bytes.insert(bytes.end(), encoded_value);
        const std::array<std::uint8_t, 6> suffix{'q', 0, 'a', 'h', 0, 'a'};
        bytes.insert(bytes.end(), suffix.begin(), suffix.end());
        bytes.push_back('.');
        return pickle::loads(std::span{
            reinterpret_cast<const std::byte *>(bytes.data()),
            bytes.size(),
        });
    }
} // namespace

TEST_CASE("pickle: scalar values")
{
    const auto none = pickle::loads(std::string{"N."});
    REQUIRE(none->get_type() == pickle::value::type::none);

    const auto true_value = load(std::array<std::uint8_t, 4>{0x80, 0x04, 0x88, '.'});
    REQUIRE(true_value->get_type() == pickle::value::type::bool_);
    REQUIRE(true_value->as_bool());

    const auto false_value = load(std::array<std::uint8_t, 4>{0x80, 0x04, 0x89, '.'});
    REQUIRE(false_value->get_type() == pickle::value::type::bool_);
    REQUIRE_FALSE(false_value->as_bool());

    const auto integer = load(std::array<std::uint8_t, 3>{'K', 42, '.'});
    REQUIRE(integer->get_type() == pickle::value::type::int64);
    REQUIRE(integer->as_int64() == 42);

    const auto float_value = load(std::array<std::uint8_t, 10>{'G', 0x3f, 0xf8, 0, 0, 0, 0, 0, 0, '.'});
    REQUIRE(float_value->as_float64() == 1.5);
}

TEST_CASE("pickle: strings and lists")
{
    const auto string = load(std::array<std::uint8_t, 6>{'U', 3, 'f', 'o', 'o', '.'});
    REQUIRE(string->get_type() == pickle::value::type::string);
    REQUIRE(string->as_string() == "foo");

    const auto list = load(std::array<std::uint8_t, 8>{']', '(', 'K', 1, 'K', 2, 'e', '.'});
    REQUIRE(list->get_type() == pickle::value::type::list);
    REQUIRE(list->as_list().size() == 2);
    REQUIRE(list->as_list()[0]->as_int64() == 1);
    REQUIRE(list->as_list()[1]->as_int64() == 2);
}

TEST_CASE("pickle: integer and protocol encodings")
{
    REQUIRE(load({'J', 0x78, 0x56, 0x34, 0x12, '.'})->as_int64() == 0x12345678);
    REQUIRE(load({'J', 0xff, 0xff, 0xff, 0xff, '.'})->as_int64() == -1);
    REQUIRE(load({'M', 0x34, 0x12, '.'})->as_int64() == 0x1234);
    REQUIRE(load({'I', '4', '2', '\n', '.'})->as_int64() == 42);
    REQUIRE(load({'I', '-', '4', '2', 'L', '\n', '.'})->as_int64() == -42);

    REQUIRE(load({0x80, 4, 0x95, 0, 0, 0, 0, 0, 0, 0, 0, 'K', 7, '.'})->as_int64() == 7);
}

TEST_CASE("pickle: string and byte encodings")
{
    const auto bin_string = load({'T', 3, 0, 0, 0, 'f', 'o', 'o', '.'});
    REQUIRE(bin_string->get_type() == pickle::value::type::string);
    REQUIRE(bin_string->as_string() == "foo");

    REQUIRE(load({0x8c, 3, 'b', 'a', 'r', '.'})->as_string() == "bar");
    REQUIRE(load({'X', 3, 0, 0, 0, 'b', 'a', 'z', '.'})->as_string() == "baz");

    const auto short_bytes = load({'C', 2, 0, 0xff, '.'});
    REQUIRE(short_bytes->get_type() == pickle::value::type::bytes);
    REQUIRE(short_bytes->as_string() == std::string{"\0\xff", 2});

    const auto bin_bytes = load({'B', 3, 0, 0, 0, 1, 2, 3, '.'});
    REQUIRE(bin_bytes->get_type() == pickle::value::type::bytes);
    REQUIRE(bin_bytes->as_string() == std::string{"\1\2\3", 3});
}

TEST_CASE("pickle: list and tuple encodings")
{
    REQUIRE(load({']', 'K', 1, 'a', '.'})->as_list()[0]->as_int64() == 1);
    REQUIRE(load({'(', 'K', 1, 'K', 2, 'l', '.'})->as_list().size() == 2);

    REQUIRE(load({')', '.'})->as_tuple().empty());
    REQUIRE(load({'(', 'K', 1, 't', '.'})->as_tuple()[0]->as_int64() == 1);
    REQUIRE(load({'K', 1, 0x85, '.'})->as_tuple()[0]->as_int64() == 1);

    const auto tuple2 = load({'K', 1, 'K', 2, 0x86, '.'});
    REQUIRE(tuple2->as_tuple().size() == 2);
    REQUIRE(tuple2->as_tuple()[1]->as_int64() == 2);

    const auto tuple3 = load({'K', 1, 'K', 2, 'K', 3, 0x87, '.'});
    REQUIRE(tuple3->as_tuple().size() == 3);
    REQUIRE(tuple3->as_tuple()[2]->as_int64() == 3);
}

TEST_CASE("pickle: dictionary encodings")
{
    REQUIRE(load({'}', '.'})->as_dict().empty());

    const auto dict = load({'(', 'U', 1, 'a', 'K', 1, 'd', '.'});
    REQUIRE(dict->as_dict().at("a")->as_int64() == 1);

    const auto setitem = load({'}', 'U', 1, 'a', 'K', 2, 's', '.'});
    REQUIRE(setitem->as_dict().at("a")->as_int64() == 2);

    const auto setitems = load({'}', '(', 'U', 1, 'a', 'K', 3, 'U', 1, 'b', 'K', 4, 'u', '.'});
    REQUIRE(setitems->as_dict().size() == 2);
    REQUIRE(setitems->as_dict().at("a")->as_int64() == 3);
    REQUIRE(setitems->as_dict().at("b")->as_int64() == 4);
}

TEST_CASE("pickle: LONG1 integers")
{
    REQUIRE(load(std::array<std::uint8_t, 3>{0x8a, 0, '.'})->as_int64() == 0);
    REQUIRE(load(std::array<std::uint8_t, 4>{0x8a, 1, 0x7f, '.'})->as_int64() == 127);
    REQUIRE(load(std::array<std::uint8_t, 4>{0x8a, 1, 0xff, '.'})->as_int64() == -1);
    REQUIRE(load({0x8a, 8, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0x7f, '.'})->as_int64() == INT64_MAX);

    REQUIRE_THROWS(load(std::array<std::uint8_t, 12>{0x8a, 9, 0, 0, 0, 0, 0, 0, 0, 0, 0, '.'}));
}

TEST_CASE("pickle: malformed input is rejected")
{
    REQUIRE_THROWS(pickle::loads(std::string{}));
    REQUIRE_THROWS(pickle::loads(std::string{"?."}));
    REQUIRE_THROWS(pickle::loads(std::string{"I\n"}));

    REQUIRE_THROWS(load({'M', 1}));
    REQUIRE_THROWS(load({'J', 1, 2, 3}));
    REQUIRE_THROWS(load({'G', 0, 0, 0, 0, 0, 0, 0}));
    REQUIRE_THROWS(load({'U', 2, 'x'}));
    REQUIRE_THROWS(load({'I', '1'}));
    REQUIRE_THROWS(load({'N', 'N', '.'}));

    REQUIRE_THROWS(load({'a'}));
    REQUIRE_THROWS(load({'N', 'K', 1, 'a'}));
    REQUIRE_THROWS(load({'(', 'K', 1, 'e'}));
    REQUIRE_THROWS_AS(load({']', 'N', '(', 'a', 'l'}), std::runtime_error);
    REQUIRE_THROWS(load({'N', '(', 'K', 1, 'e'}));
    REQUIRE_THROWS(load({'l'}));

    REQUIRE_THROWS(load({0x85}));
    REQUIRE_THROWS(load({'K', 1, 0x86}));
    REQUIRE_THROWS(load({'K', 1, 'K', 2, 0x87}));

    REQUIRE_THROWS(load({'(', 'U', 1, 'a', 'd'}));
    REQUIRE_THROWS(load({'(', 'K', 1, 'K', 2, 'd'}));
    REQUIRE_THROWS(load({'s'}));
    REQUIRE_THROWS(load({'N', 'U', 1, 'a', 'K', 1, 's'}));
    REQUIRE_THROWS(load({'}', 'K', 1, 'K', 2, 's'}));
    REQUIRE_THROWS(load({'}', '(', 'U', 1, 'a', 'u'}));
    REQUIRE_THROWS(load({'(', 'U', 1, 'a', 'K', 1, 'u'}));
    REQUIRE_THROWS(load({'N', '(', 'U', 1, 'a', 'K', 1, 'u'}));
    REQUIRE_THROWS(load({'}', '(', 'K', 1, 'K', 2, 'u'}));

    REQUIRE_THROWS(load({'q'}));
    REQUIRE_THROWS(load({'q', 0}));
    REQUIRE_THROWS(load({'r', 0, 0, 0, 0}));
    REQUIRE_THROWS(load({'h', 0}));
    REQUIRE_THROWS(load({'j', 0, 0, 0, 0}));
    REQUIRE_THROWS(load({0x94}));
    REQUIRE_THROWS(load({0x80}));
    REQUIRE_THROWS(load({0x95, 0, 0, 0, 0, 0, 0, 0}));
    REQUIRE_THROWS(load({0x8a}));
    REQUIRE_THROWS(load({0x8a, 1}));

    const auto none = pickle::value::none();
    REQUIRE_THROWS(none->as_bool());
    REQUIRE_THROWS(none->as_int64());
    REQUIRE_THROWS(none->as_float64());
    REQUIRE_THROWS(none->as_string());
    REQUIRE_THROWS(none->as_list());
    REQUIRE_THROWS(none->as_tuple());
    REQUIRE_THROWS(none->as_dict());
}

TEST_CASE("pickle: memo references preserve values")
{
    const auto list = load(std::array<std::uint8_t, 10>{']', 'K', 42, 'q', 7, 'a', 'h', 7, 'a', '.'});

    REQUIRE(list->as_list().size() == 2);
    REQUIRE(list->as_list()[0]->as_int64() == 42);
    REQUIRE(list->as_list()[1]->as_int64() == 42);

    const auto long_memo = load({']', 'K', 9, 'r', 1, 0, 0, 0, 'a', 'j', 1, 0, 0, 0, 'a', '.'});
    REQUIRE(long_memo->as_list()[1]->as_int64() == 9);

    const auto auto_memo = load({']', 'K', 8, 0x94, 'a', 'h', 0, 'a', '.'});
    REQUIRE(auto_memo->as_list()[1]->as_int64() == 8);
}

TEST_CASE("pickle: memo copies all supported value types")
{
    REQUIRE(load_memo_copy({'N'})->as_list()[1]->get_type() == pickle::value::type::none);
    REQUIRE(load_memo_copy({0x88})->as_list()[1]->as_bool());
    REQUIRE(load_memo_copy({'K', 2})->as_list()[1]->as_int64() == 2);
    REQUIRE(load_memo_copy({'G', 0x3f, 0xf0, 0, 0, 0, 0, 0, 0})->as_list()[1]->as_float64() == 1.0);
    REQUIRE(load_memo_copy({'C', 1, 'b'})->as_list()[1]->get_type() == pickle::value::type::bytes);
    REQUIRE(load_memo_copy({'U', 1, 's'})->as_list()[1]->get_type() == pickle::value::type::string);
    REQUIRE(load_memo_copy({']', 'K', 1, 'a'})->as_list()[1]->as_list()[0]->as_int64() == 1);
    REQUIRE(load_memo_copy({'}', 'U', 1, 'k', 'K', 1, 's'})->as_list()[1]->as_dict().at("k")->as_int64() == 1);
    REQUIRE(load_memo_copy({'K', 1, 0x85})->as_list()[1]->as_tuple()[0]->as_int64() == 1);

    const pickle::value invalid(std::bit_cast<pickle::value::type>(UINT8_MAX));
    REQUIRE_THROWS(pickle::clone(invalid));
}

TEST_CASE("archive: typed failures are standard exceptions")
{
    REQUIRE(std::string_view(archive::read_error{}.what()).empty());
    REQUIRE(std::string_view(archive::write_error{}.what()).empty());
    REQUIRE(std::string_view(archive::user_interrupt{}.what()).empty());
    REQUIRE(std::string_view(extractor::read_error{}.what()).empty());

    const auto base = std::make_unique<extractor::extractor>();
    REQUIRE(base != nullptr);
}
