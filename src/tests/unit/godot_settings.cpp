#include "../../modules/godot/settings.h"

#include <algorithm>
#include <cstddef>
#include <string>
#include <vector>

#include <catch2/catch_test_macros.hpp>

namespace
{
    using unpackrat::godot::parse_module_settings;
}

TEST_CASE("godot settings: null missing and empty settings disable resource decoding", "[godot][decode-settings]")
{
    CHECK(!parse_module_settings(nullptr).decode_resources);
    CHECK(!parse_module_settings(L"\0").decode_resources);
    CHECK(!parse_module_settings(L"Other=1\0").decode_resources);
}

TEST_CASE("godot settings: the host MULTISZ key enables only the explicit one value", "[godot][decode-settings]")
{
    CHECK(parse_module_settings(L"DecodeResources=1\0").decode_resources);
    CHECK(parse_module_settings(L"Other=0\0DecodeResources=1\0Tail=value\0").decode_resources);
    CHECK(parse_module_settings(L"decodEResources= \t1 \t\0").decode_resources);
    CHECK(!parse_module_settings(L"DecodeResources=0\0").decode_resources);
    CHECK(!parse_module_settings(L"DecodeResources=2\0").decode_resources);
    CHECK(!parse_module_settings(L"DecodeResources=true\0").decode_resources);
    CHECK(!parse_module_settings(L"DecodeResources=\0").decode_resources);
    CHECK(!parse_module_settings(L"DecodeResources\0").decode_resources);
    CHECK(!parse_module_settings(L"DecodeResourcesExtra=1\0").decode_resources);
    CHECK(!parse_module_settings(L"DecodeResource=1\0").decode_resources);
    CHECK(!parse_module_settings(L"DecodeResourceX=1\0").decode_resources);
    CHECK(!parse_module_settings(L"DecodeResource1=1\0").decode_resources);
    CHECK(!parse_module_settings(L"Other=DecodeResources=1\0").decode_resources);
}

TEST_CASE("godot settings: duplicate keys follow the first INI occurrence", "[godot][decode-settings]")
{
    CHECK(parse_module_settings(L"DecodeResources=1\0DecodeResources=0\0").decode_resources);
    CHECK(!parse_module_settings(L"DecodeResources=0\0DecodeResources=1\0").decode_resources);
}

TEST_CASE("godot settings: malformed and over-bound sections cannot enable decoding", "[godot][decode-settings]")
{
    std::vector<wchar_t> unterminated(unpackrat::godot::module_settings_max_chars, L'x');
    CHECK(!parse_module_settings(unterminated.data()).decode_resources);
    const std::wstring first = L"DecodeResources=1";
    std::copy(first.begin(), first.end(), unterminated.begin());
    unterminated[first.size()] = L'\0';
    CHECK(!parse_module_settings(unterminated.data()).decode_resources);
    unterminated[unterminated.size() - 1] = L'\0';
    CHECK(!parse_module_settings(unterminated.data()).decode_resources);
    unterminated[unterminated.size() - 2] = L'\0';
    CHECK(parse_module_settings(unterminated.data()).decode_resources);
}
