#include "import_remap.h"

#include <cstdint>
#include <optional>
#include <string_view>
#include <utility>

#include "../archive_limits.h"

namespace unpackrat::godot
{
    namespace
    {
        constexpr std::string_view import_suffix = ".import";
        constexpr std::string_view remap_suffix = ".remap";

        bool is_inline_space(const char c) noexcept
        {
            switch (c) {
            case ' ':
            case '\t':
                return true;
            default:
                return false;
            }
        }

        bool is_newline(const char c) noexcept
        {
            switch (c) {
            case '\n':
            case '\r':
                return true;
            default:
                return false;
            }
        }

        bool is_trivia_space(const char c) noexcept
        {
            switch (c) {
            case ' ':
            case '\t':
            case '\r':
            case '\n':
                return true;
            default:
                return false;
            }
        }

        // Trim leading and trailing ConfigFile whitespace, matching the official
        // simple-tag ``strip_edges`` step.
        std::string_view trim_edges(const std::string_view text) noexcept
        {
            std::size_t begin = 0;
            std::size_t end = text.size();
            while (begin < end) {
                if (!is_trivia_space(text[begin])) {
                    break;
                }
                ++begin;
            }
            while (end > begin) {
                if (!is_trivia_space(text[end - 1])) {
                    break;
                }
                --end;
            }
            return text.substr(begin, end - begin);
        }

        // A ConfigFile section header escapes ``]`` as ``\]``; decode it in one
        // forward pass so a name carrying many escapes stays linear instead of
        // the quadratic repeated in-place replacement it used to do.
        std::string unescape_section_name(const std::string_view name)
        {
            std::string out;
            out.reserve(name.size());
            for (std::size_t index = 0; index < name.size(); ++index) {
                if (name[index] == '\\' && index + 1 < name.size() && name[index + 1] == ']') {
                    out.push_back(']');
                    ++index;
                    continue;
                }
                out.push_back(name[index]);
            }
            return out;
        }

        // Characters that cannot appear unquoted in a ConfigFile key.
        bool is_key_punctuation(const char c) noexcept
        {
            switch (c) {
            case '[':
            case ']':
            case '{':
            case '}':
            case '"':
            case ';':
                return true;
            default:
                return false;
            }
        }

        // A quoted string may carry a one-character type prefix: ``&"..."`` is a
        // StringName and ``^"..."`` a NodePath. Either is a complete value on its
        // own, matching the official VariantParser token handling.
        bool is_value_prefix(const char c) noexcept
        {
            return c == '&' || c == '^';
        }

        // Characters that may appear in an unquoted constructor name such as
        // ``Vector2`` or ``Transform2D``.
        bool is_identifier_char(const char c) noexcept
        {
            if (c >= '0' && c <= '9') {
                return true;
            }
            if (c >= 'A' && c <= 'Z') {
                return true;
            }
            if (c >= 'a' && c <= 'z') {
                return true;
            }
            return c == '_';
        }

        std::int32_t hex_value(const char c) noexcept
        {
            if (c >= '0' && c <= '9') {
                return c - '0';
            }
            if (c >= 'a' && c <= 'f') {
                return c - 'a' + 10;
            }
            if (c >= 'A' && c <= 'F') {
                return c - 'A' + 10;
            }
            return -1;
        }

        std::string encode_utf8(const std::uint32_t code_point)
        {
            std::string out;
            if (code_point < 0x80U) {
                out.push_back(static_cast<char>(code_point));
            } else if (code_point < 0x800U) {
                out.push_back(static_cast<char>(0xC0U | (code_point >> 6U)));
                out.push_back(static_cast<char>(0x80U | (code_point & 0x3FU)));
            } else if (code_point < 0x10000U) {
                out.push_back(static_cast<char>(0xE0U | (code_point >> 12U)));
                out.push_back(static_cast<char>(0x80U | ((code_point >> 6U) & 0x3FU)));
                out.push_back(static_cast<char>(0x80U | (code_point & 0x3FU)));
            } else {
                out.push_back(static_cast<char>(0xF0U | (code_point >> 18U)));
                out.push_back(static_cast<char>(0x80U | ((code_point >> 12U) & 0x3FU)));
                out.push_back(static_cast<char>(0x80U | ((code_point >> 6U) & 0x3FU)));
                out.push_back(static_cast<char>(0x80U | (code_point & 0x3FU)));
            }
            return out;
        }

        // Normalize to an archive-relative, slash-separated path. Returns nullopt
        // for absolute, traversal, control-character or over-budget values.
        //
        // ``raw`` is an untrusted borrowed view. Normalization can only shrink a
        // value (prefix stripping, dot-segment removal and separator collapsing),
        // so any input longer than the output cap is rejected before a single
        // byte is copied or appended.
        std::optional<std::string> normalize_path(const std::string_view raw)
        {
            if (raw.size() > archive_limits::max_path_bytes) {
                return std::nullopt;
            }
            std::string_view value = raw;
            if (value.starts_with("res://")) {
                value.remove_prefix(6);
            }
            if (value.starts_with('/')) {
                return std::nullopt;
            }
            if (value.starts_with('\\')) {
                return std::nullopt;
            }
            for (const char c : value) {
                if (static_cast<unsigned char>(c) < 0x20U) {
                    return std::nullopt;
                }
                if (c == ':') {
                    return std::nullopt;
                }
            }

            std::string normalized;
            std::string segment;
            const auto flush = [&normalized, &segment]() {
                if (segment == "..") {
                    return false;
                }
                if (!segment.empty()) {
                    if (segment != ".") {
                        if (!normalized.empty()) {
                            normalized.push_back('/');
                        }
                        normalized += segment;
                    }
                }
                return true;
            };
            for (const char c : value) {
                if (c == '/') {
                    if (!flush()) {
                        return std::nullopt;
                    }
                    segment.clear();
                    continue;
                }
                if (c == '\\') {
                    if (!flush()) {
                        return std::nullopt;
                    }
                    segment.clear();
                    continue;
                }
                segment.push_back(c);
            }
            if (!flush()) {
                return std::nullopt;
            }
            if (normalized.empty()) {
                return std::nullopt;
            }
            return normalized;
        }

        class scanner
        {
          public:
            struct found
            {
                bool has_source_file = false;
                std::string source_file;
                std::vector<std::pair<std::string, std::string>> paths;
            };

            scanner(const std::string_view text, const metadata_limits &limits) : text_(text), limits_(limits)
            {
            }

            found scan()
            {
                std::string section;
                for (;;) {
                    skip_trivia();
                    if (at_end()) {
                        return result_;
                    }
                    if (peek() == '[') {
                        section = parse_section_name();
                        continue;
                    }
                    const std::string key = parse_key();
                    skip_inline_spaces();
                    if (at_end()) {
                        throw metadata_error{};
                    }
                    if (peek() != '=') {
                        throw metadata_error{};
                    }
                    ++position_;
                    skip_inline_spaces();
                    record(section, key, parse_value());
                }
            }

          private:
            std::string parse_section_name()
            {
                ++position_; // consume '['
                std::string name;
                bool escaping = false;
                for (;;) {
                    if (at_end()) {
                        throw metadata_error{};
                    }
                    const char c = take();
                    if (c == ']' && !escaping) {
                        break;
                    }
                    if (is_newline(c)) {
                        throw metadata_error{};
                    }
                    escaping = (c == '\\');
                    name.push_back(c);
                }
                return unescape_section_name(trim_edges(name));
            }

            std::string parse_key()
            {
                // ConfigFile permits quoted keys; the decoded string is the key.
                if (peek() == '"') {
                    return parse_string();
                }
                std::string key;
                for (;;) {
                    if (at_end()) {
                        throw metadata_error{};
                    }
                    const char c = peek();
                    if (c == '=') {
                        break;
                    }
                    if (is_inline_space(c)) {
                        break;
                    }
                    if (is_newline(c)) {
                        throw metadata_error{};
                    }
                    if (is_key_punctuation(c)) {
                        throw metadata_error{};
                    }
                    key.push_back(c);
                    ++position_;
                }
                if (key.empty()) {
                    throw metadata_error{};
                }
                return key;
            }

            std::optional<std::string> parse_value()
            {
                if (at_end()) {
                    throw metadata_error{};
                }
                const char c = peek();
                if (c == '"') {
                    return parse_string();
                }
                if (c == '[' || c == '{' || c == '(') {
                    skip_balanced();
                    return std::nullopt;
                }
                if (is_newline(c)) {
                    throw metadata_error{};
                }
                if (is_value_prefix(c)) {
                    ++position_;
                    if (at_end() || peek() != '"') {
                        throw metadata_error{};
                    }
                    parse_string();
                    return std::nullopt;
                }
                if (is_identifier_char(c) && is_constructor_call()) {
                    skip_balanced();
                    return std::nullopt;
                }
                skip_scalar_token();
                return std::nullopt;
            }

            std::string parse_string()
            {
                ++position_; // consume '"'
                std::string out;
                std::uint32_t pending_lead = 0;
                for (;;) {
                    if (at_end()) {
                        throw metadata_error{};
                    }
                    const char c = take();
                    if (c == '"') {
                        if (pending_lead != 0) {
                            throw metadata_error{};
                        }
                        return out;
                    }
                    if (c == '\\') {
                        append_escape(out, pending_lead);
                        continue;
                    }
                    if (pending_lead != 0) {
                        throw metadata_error{};
                    }
                    out.push_back(c);
                }
            }

            // Decode one escape and merge UTF-16 surrogate pairs exactly as the
            // official VariantParser does: six hex digits for ``\U``, four for
            // ``\u``, and any other escaped character stands for itself.
            void append_escape(std::string &out, std::uint32_t &pending_lead)
            {
                const std::uint32_t value = parse_escape_value();
                if ((value & 0xFFFFFC00U) == 0xD800U) {
                    if (pending_lead != 0) {
                        throw metadata_error{};
                    }
                    pending_lead = value;
                    return;
                }
                if ((value & 0xFFFFFC00U) == 0xDC00U) {
                    if (pending_lead == 0) {
                        throw metadata_error{};
                    }
                    const std::uint32_t code_point =
                        (pending_lead << 10U) + value - ((0xD800U << 10U) + 0xDC00U - 0x10000U);
                    pending_lead = 0;
                    out += encode_utf8(code_point);
                    return;
                }
                if (pending_lead != 0) {
                    throw metadata_error{};
                }
                if (value > 0x10FFFFU) {
                    throw metadata_error{};
                }
                out += encode_utf8(value);
            }

            std::uint32_t parse_escape_value()
            {
                if (at_end()) {
                    throw metadata_error{};
                }
                const char c = take();
                switch (c) {
                case 'b':
                    return 8;
                case 't':
                    return 9;
                case 'n':
                    return 10;
                case 'f':
                    return 12;
                case 'r':
                    return 13;
                case 'u':
                    return parse_hex_escape(4);
                case 'U':
                    return parse_hex_escape(6);
                default:
                    return static_cast<unsigned char>(c);
                }
            }

            std::uint32_t parse_hex_escape(const std::size_t digits)
            {
                std::uint32_t value = 0;
                for (std::size_t index = 0; index < digits; ++index) {
                    if (at_end()) {
                        throw metadata_error{};
                    }
                    const std::int32_t digit = hex_value(take());
                    if (digit < 0) {
                        throw metadata_error{};
                    }
                    value = (value << 4U) | static_cast<std::uint32_t>(digit);
                }
                return value;
            }

            void skip_balanced()
            {
                std::string stack;
                stack.push_back(peek());
                ++position_;
                if (stack.size() > limits_.max_nesting) {
                    throw metadata_error{};
                }
                while (!stack.empty()) {
                    if (at_end()) {
                        throw metadata_error{};
                    }
                    const char c = peek();
                    if (c == '"') {
                        parse_string();
                        continue;
                    }
                    if (c == ';') {
                        skip_comment();
                        continue;
                    }
                    if (c == '[') {
                        push_bracket(stack, c);
                        continue;
                    }
                    if (c == '{') {
                        push_bracket(stack, c);
                        continue;
                    }
                    if (c == '(') {
                        push_bracket(stack, c);
                        continue;
                    }
                    if (c == ']') {
                        pop_bracket(stack, c);
                        continue;
                    }
                    if (c == '}') {
                        pop_bracket(stack, c);
                        continue;
                    }
                    if (c == ')') {
                        pop_bracket(stack, c);
                        continue;
                    }
                    ++position_;
                }
            }

            void push_bracket(std::string &stack, const char open)
            {
                stack.push_back(open);
                ++position_;
                if (stack.size() > limits_.max_nesting) {
                    throw metadata_error{};
                }
            }

            void pop_bracket(std::string &stack, const char close)
            {
                const char open = stack.back();
                char expected = '(';
                if (close == ']') {
                    expected = '[';
                } else if (close == '}') {
                    expected = '{';
                }
                if (open != expected) {
                    throw metadata_error{};
                }
                stack.pop_back();
                ++position_;
            }

            // Advance ``index`` past ConfigFile trivia (inline spaces, newlines
            // and ``;`` comments) and report whether a significant character
            // remains at or after ``index``.
            bool skip_value_trivia(std::size_t &index) const noexcept
            {
                for (;;) {
                    if (index >= text_.size()) {
                        return false;
                    }
                    const char c = text_[index];
                    if (is_trivia_space(c)) {
                        ++index;
                        continue;
                    }
                    if (c != ';') {
                        return true;
                    }
                    ++index;
                    while (index < text_.size() && text_[index] != '\n') {
                        ++index;
                    }
                }
            }

            // A bare identifier may name a constructor such as ``Vector2(...)``.
            // ConfigFile trivia (inline spaces, newlines and ``;`` comments) may
            // separate the name from its opening parenthesis, so probe past that
            // trivia without moving the cursor. On a match ``position_`` advances
            // to the ``(`` and the caller consumes the balanced group; otherwise
            // the cursor stays put and the identifier is read as a scalar token.
            bool is_constructor_call() noexcept
            {
                std::size_t probe = position_;
                while (probe < text_.size() && is_identifier_char(text_[probe])) {
                    ++probe;
                }
                if (!skip_value_trivia(probe)) {
                    return false;
                }
                if (text_[probe] != '(') {
                    return false;
                }
                position_ = probe;
                return true;
            }

            // Consume exactly one bare lexical token, stopping at the first
            // ConfigFile delimiter: inline space, newline or ``;`` comment. A
            // scalar never spans whitespace and never swallows a following
            // assignment; an empty token is malformed.
            void skip_scalar_token()
            {
                const std::size_t start = position_;
                while (position_ < text_.size()) {
                    const char c = text_[position_];
                    if (is_trivia_space(c) || c == ';') {
                        break;
                    }
                    ++position_;
                }
                if (position_ == start) {
                    throw metadata_error{};
                }
            }

            void skip_comment()
            {
                ++position_;
                for (;;) {
                    if (at_end()) {
                        break;
                    }
                    if (peek() == '\n') {
                        break;
                    }
                    ++position_;
                }
            }

            void skip_trivia()
            {
                for (;;) {
                    if (at_end()) {
                        return;
                    }
                    const char c = peek();
                    if (is_trivia_space(c)) {
                        ++position_;
                        continue;
                    }
                    if (c == ';') {
                        skip_comment();
                        continue;
                    }
                    return;
                }
            }

            void skip_inline_spaces()
            {
                while (!at_end()) {
                    if (!is_inline_space(peek())) {
                        break;
                    }
                    ++position_;
                }
            }

            void record(const std::string &section, const std::string &key, std::optional<std::string> value)
            {
                if (section == "remap") {
                    record_remap(key, std::move(value));
                    return;
                }
                if (section == "deps") {
                    record_deps(key, std::move(value));
                }
            }

            void record_remap(const std::string &key, std::optional<std::string> value)
            {
                if (key == "path") {
                    add_path(std::string{}, require_string(std::move(value)));
                    return;
                }
                if (key.starts_with("path.")) {
                    std::string variant = key.substr(5);
                    if (variant.empty()) {
                        throw metadata_error{};
                    }
                    add_path(std::move(variant), require_string(std::move(value)));
                }
            }

            void record_deps(const std::string &key, std::optional<std::string> value)
            {
                if (key != "source_file") {
                    return;
                }
                std::string text = require_string(std::move(value));
                if (result_.has_source_file) {
                    if (result_.source_file != text) {
                        throw metadata_error{};
                    }
                    return;
                }
                result_.has_source_file = true;
                result_.source_file = std::move(text);
            }

            std::string require_string(std::optional<std::string> value)
            {
                if (!value.has_value()) {
                    throw metadata_error{};
                }
                return std::move(*value);
            }

            void add_path(std::string variant, std::string value)
            {
                for (const auto &entry : result_.paths) {
                    if (entry.first == variant) {
                        if (entry.second != value) {
                            throw metadata_error{};
                        }
                        return;
                    }
                }
                if (result_.paths.size() >= limits_.max_mappings) {
                    throw metadata_error{};
                }
                result_.paths.emplace_back(std::move(variant), std::move(value));
            }

            bool at_end() const noexcept
            {
                return position_ >= text_.size();
            }

            char peek() const noexcept
            {
                return text_[position_];
            }

            char take() noexcept
            {
                return text_[position_++];
            }

            std::string_view text_;
            const metadata_limits &limits_;
            std::size_t position_ = 0;
            found result_;
        };
    } // namespace

    std::vector<import_mapping> read_import_remap(const std::span<const std::byte> bytes,
                                                  const std::string_view metadata_path, const metadata_limits &limits)
    {
        const std::optional<std::string> metadata = normalize_path(metadata_path);
        if (!metadata.has_value()) {
            throw metadata_error{};
        }

        std::string_view stem;
        if (metadata->ends_with(import_suffix)) {
            stem = std::string_view(*metadata).substr(0, metadata->size() - import_suffix.size());
        } else if (metadata->ends_with(remap_suffix)) {
            stem = std::string_view(*metadata).substr(0, metadata->size() - remap_suffix.size());
        } else {
            throw metadata_error{};
        }

        const std::optional<std::string> derived_virtual = normalize_path(stem);
        if (!derived_virtual.has_value()) {
            throw metadata_error{};
        }

        if (bytes.size() > limits.max_input_bytes) {
            throw metadata_error{};
        }

        std::string_view text(reinterpret_cast<const char *>(bytes.data()), bytes.size());
        // Godot exports .import using CharString::size(), which includes its terminal NUL.
        // Charge the stored bytes above and remove only that terminator, never interior bytes.
        if (text.ends_with('\0')) {
            text.remove_suffix(1);
        }
        scanner reader{text, limits};
        const scanner::found found = reader.scan();

        std::string virtual_path;
        if (found.has_source_file) {
            const std::optional<std::string> normalized_source = normalize_path(found.source_file);
            if (!normalized_source.has_value()) {
                throw metadata_error{};
            }
            virtual_path = *normalized_source;
        } else {
            virtual_path = *derived_virtual;
        }

        std::vector<import_mapping> mappings;
        mappings.reserve(found.paths.size());
        for (const auto &[variant, value] : found.paths) {
            const std::optional<std::string> compiled = normalize_path(value);
            if (!compiled.has_value()) {
                throw metadata_error{};
            }
            mappings.push_back(import_mapping{virtual_path, *compiled, variant});
        }
        return mappings;
    }
} // namespace unpackrat::godot
