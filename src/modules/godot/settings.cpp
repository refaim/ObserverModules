#include "settings.h"

#include <string_view>

namespace unpackrat::godot
{
    namespace
    {
        bool is_decode_key(const std::wstring_view key) noexcept
        {
            constexpr std::wstring_view expected = L"decoderesources";
            if (key.size() != expected.size()) {
                return false;
            }
            for (std::size_t index = 0; index < key.size(); ++index) {
                const wchar_t letter = key[index];
                const wchar_t folded =
                    letter >= L'A' && letter <= L'Z' ? static_cast<wchar_t>(letter + (L'a' - L'A')) : letter;
                if (folded != expected[index]) {
                    return false;
                }
            }
            return true;
        }

        std::wstring_view trim_value(const std::wstring_view value) noexcept
        {
            const std::size_t first = value.find_first_not_of(L" \t");
            if (first == std::wstring_view::npos) {
                return {};
            }
            return value.substr(first, value.find_last_not_of(L" \t") - first + 1);
        }
    } // namespace

    module_settings parse_module_settings(const wchar_t *multi_sz) noexcept
    {
        if (multi_sz == nullptr) {
            return {};
        }
        module_settings result;
        bool found = false;
        std::size_t cursor = 0;
        while (cursor < module_settings_max_chars) {
            const std::size_t begin = cursor;
            while (cursor < module_settings_max_chars && multi_sz[cursor] != L'\0') {
                ++cursor;
            }
            if (cursor == module_settings_max_chars) {
                return {};
            }
            if (cursor == begin) {
                return result; // empty section or the second terminator after a complete entry
            }
            const std::wstring_view entry(multi_sz + begin, cursor - begin);
            const auto separator = entry.find(L'=');
            if (!found && separator != std::wstring_view::npos && is_decode_key(entry.substr(0, separator))) {
                found = true;
                result.decode_resources = trim_value(entry.substr(separator + 1)) == L"1";
            }
            ++cursor;
        }
        return {}; // no complete double-NUL section within the host-input bound
    }
} // namespace unpackrat::godot
