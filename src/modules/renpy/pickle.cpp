#include "pickle.h"

#include <algorithm>
#include <bit>
#include <iterator>
#include <numeric>

namespace pickle
{
    namespace
    {
        list clone_list(const list &source)
        {
            list result;
            result.reserve(source.size());
            std::ranges::transform(source, std::back_inserter(result), [](const auto &item) { return clone(*item); });
            return result;
        }

        dict clone_dict(const dict &source)
        {
            dict result;
            result.reserve(source.size());
            for (const auto &[key, item] : source) {
                result.emplace(key, clone(*item));
            }
            return result;
        }
    } // namespace

    value_ptr clone(const value &source)
    {
        switch (source.get_type()) {
        case value::type::none:
            return value::none();
        case value::type::bool_:
            return value::boolean(source.as_bool());
        case value::type::int64:
            return value::int64(source.as_int64());
        case value::type::float64:
            return value::float64(source.as_float64());
        case value::type::bytes:
            return value::bytes(source.as_string());
        case value::type::string:
            return value::string(source.as_string());
        case value::type::list:
            return value::list(clone_list(source.as_list()));
        case value::type::dict:
            return value::dict(clone_dict(source.as_dict()));
        case value::type::tuple:
            return value::tuple(clone_list(source.as_tuple()));
        default:
            throw std::runtime_error("Unsupported pickle value type");
        }
    }

    // Pickle opcodes (subset needed for basic functionality)
    namespace opcodes
    {
        constexpr uint8_t MARK = '(';
        constexpr uint8_t STOP = '.';
        constexpr uint8_t INT = 'I';
        constexpr uint8_t BININT = 'J';
        constexpr uint8_t BININT1 = 'K';
        constexpr uint8_t BININT2 = 'M';
        constexpr uint8_t NONE = 'N';
        constexpr uint8_t BINSTRING = 'T';
        constexpr uint8_t SHORT_BINSTRING = 'U';
        constexpr uint8_t BINUNICODE = 'X';
        constexpr uint8_t APPEND = 'a';
        constexpr uint8_t DICT = 'd';
        constexpr uint8_t EMPTY_DICT = '}';
        constexpr uint8_t APPENDS = 'e';
        constexpr uint8_t BINGET = 'h';
        constexpr uint8_t LONG_BINGET = 'j';
        constexpr uint8_t LIST = 'l';
        constexpr uint8_t EMPTY_LIST = ']';
        constexpr uint8_t BINPUT = 'q';
        constexpr uint8_t LONG_BINPUT = 'r';
        constexpr uint8_t SETITEM = 's';
        constexpr uint8_t TUPLE = 't';
        constexpr uint8_t EMPTY_TUPLE = ')';
        constexpr uint8_t SETITEMS = 'u';
        constexpr uint8_t BINFLOAT = 'G';

        // Protocol 2
        constexpr uint8_t PROTO = 0x80;
        constexpr uint8_t TUPLE1 = 0x85;
        constexpr uint8_t TUPLE2 = 0x86;
        constexpr uint8_t TUPLE3 = 0x87;
        constexpr uint8_t NEWTRUE = 0x88;
        constexpr uint8_t NEWFALSE = 0x89;
        constexpr uint8_t LONG1 = 0x8a;

        // Protocol 3
        constexpr uint8_t BINBYTES = 'B';
        constexpr uint8_t SHORT_BINBYTES = 'C';

        // Protocol 4
        constexpr uint8_t SHORT_BINUNICODE = 0x8c;
        constexpr uint8_t MEMOIZE = 0x94;
        constexpr uint8_t FRAME = 0x95;
    } // namespace opcodes

    uint8_t parser::read_byte()
    {
        if (pos_ >= data_.size()) {
            throw std::runtime_error("Unexpected end of pickle data");
        }
        return static_cast<uint8_t>(data_[pos_++]);
    }

    uint16_t parser::read_uint16_le()
    {
        if (pos_ + 2 > data_.size()) {
            throw std::runtime_error("Unexpected end of pickle data");
        }
        const uint16_t result = static_cast<uint16_t>(data_[pos_]) | static_cast<uint16_t>(data_[pos_ + 1]) << 8;
        pos_ += 2;
        return result;
    }

    uint32_t parser::read_uint32_le()
    {
        if (pos_ + 4 > data_.size()) {
            throw std::runtime_error("Unexpected end of pickle data");
        }
        const uint32_t result = static_cast<uint32_t>(data_[pos_]) | static_cast<uint32_t>(data_[pos_ + 1]) << 8 |
                                static_cast<uint32_t>(data_[pos_ + 2]) << 16 |
                                static_cast<uint32_t>(data_[pos_ + 3]) << 24;
        pos_ += 4;
        return result;
    }

    uint64_t parser::read_uint64_le()
    {
        if (pos_ + 8 > data_.size()) {
            throw std::runtime_error("Unexpected end of pickle data");
        }
        const uint64_t result =
            static_cast<uint64_t>(data_[pos_]) | static_cast<uint64_t>(data_[pos_ + 1]) << 8 |
            static_cast<uint64_t>(data_[pos_ + 2]) << 16 | static_cast<uint64_t>(data_[pos_ + 3]) << 24 |
            static_cast<uint64_t>(data_[pos_ + 4]) << 32 | static_cast<uint64_t>(data_[pos_ + 5]) << 40 |
            static_cast<uint64_t>(data_[pos_ + 6]) << 48 | static_cast<uint64_t>(data_[pos_ + 7]) << 56;
        pos_ += 8;
        return result;
    }

    std::string parser::read_string(const size_t length)
    {
        if (pos_ + length > data_.size()) {
            throw std::runtime_error("Unexpected end of pickle data");
        }
        std::string result(reinterpret_cast<const char *>(data_.data() + pos_), length);
        pos_ += length;
        return result;
    }

    std::string parser::read_line()
    {
        const size_t start = pos_;
        while (pos_ < data_.size() && data_[pos_] != std::byte{'\n'}) {
            pos_++;
        }
        if (pos_ >= data_.size()) {
            throw std::runtime_error("Unexpected end of pickle data");
        }
        std::string result(reinterpret_cast<const char *>(data_.data() + start), pos_ - start);
        pos_++; // skip newline
        return result;
    }

    void parser::push_mark()
    {
        mark_stack_.push_back(stack_.size());
    }

    list parser::pop_to_mark()
    {
        if (mark_stack_.empty()) {
            throw std::runtime_error("No mark on stack");
        }
        const size_t mark_pos = mark_stack_.back();
        if (mark_pos > stack_.size()) {
            throw std::runtime_error("Invalid mark position");
        }
        mark_stack_.pop_back();

        list result;
        result.reserve(stack_.size() - mark_pos);
        for (size_t i = mark_pos; i < stack_.size(); ++i) {
            result.push_back(std::move(stack_[i]));
        }
        stack_.resize(mark_pos);
        return result;
    }

    value_ptr parser::parse_value()
    {
        switch (uint8_t opcode = read_byte()) {
        case opcodes::MARK:
            push_mark();
            break;

        case opcodes::STOP:
            if (stack_.size() != 1) {
                throw std::runtime_error("Invalid stack size at end of pickle");
            }
            return std::move(stack_[0]);

        case opcodes::NONE:
            stack_.push_back(value::none());
            break;

        case opcodes::NEWTRUE:
            stack_.push_back(value::boolean(true));
            break;

        case opcodes::NEWFALSE:
            stack_.push_back(value::boolean(false));
            break;

        case opcodes::BININT:
            stack_.push_back(value::int64(static_cast<int32_t>(read_uint32_le())));
            break;

        case opcodes::BININT1:
            stack_.push_back(value::int64(read_byte()));
            break;

        case opcodes::BININT2:
            stack_.push_back(value::int64(read_uint16_le()));
            break;

        case opcodes::INT: {
            std::string int_str = read_line();
            if (int_str.empty()) {
                throw std::runtime_error("Empty INT opcode argument");
            }
            if (int_str.back() == 'L') {
                int_str.pop_back(); // Remove trailing L
            }
            int64_t val = std::stoll(int_str);
            stack_.push_back(value::int64(val));
            break;
        }

        case opcodes::BINFLOAT: {
            const uint64_t bits = std::byteswap(read_uint64_le());
            double val;
            std::memcpy(&val, &bits, sizeof(double));
            stack_.push_back(value::float64(val));
            break;
        }

        case opcodes::SHORT_BINSTRING: {
            uint8_t length = read_byte();
            stack_.push_back(value::string(read_string(length)));
            break;
        }

        case opcodes::BINSTRING: {
            uint32_t length = read_uint32_le();
            stack_.push_back(value::string(read_string(length)));
            break;
        }

        case opcodes::SHORT_BINUNICODE: {
            uint8_t length = read_byte();
            stack_.push_back(value::string(read_string(length)));
            break;
        }

        case opcodes::BINUNICODE: {
            uint32_t length = read_uint32_le();
            stack_.push_back(value::string(read_string(length)));
            break;
        }

        case opcodes::SHORT_BINBYTES: {
            uint8_t length = read_byte();
            stack_.push_back(value::bytes(read_string(length)));
            break;
        }

        case opcodes::BINBYTES: {
            uint32_t length = read_uint32_le();
            stack_.push_back(value::bytes(read_string(length)));
            break;
        }

        case opcodes::EMPTY_LIST:
            stack_.push_back(value::list({}));
            break;

        case opcodes::APPEND: {
            if (stack_.size() < 2) {
                throw std::runtime_error("Not enough items on stack for APPEND");
            }
            auto item = std::move(stack_.back());
            stack_.pop_back();
            const auto &list_val = stack_.back();
            if (list_val->get_type() != value::type::list) {
                throw std::runtime_error("APPEND target is not a list");
            }
            auto &list_data = const_cast<list &>(list_val->as_list());
            list_data.push_back(std::move(item));
            break;
        }

        case opcodes::APPENDS: {
            auto items = pop_to_mark();
            if (stack_.empty()) {
                throw std::runtime_error("No list on stack for APPENDS");
            }
            const auto &list_val = stack_.back();
            if (list_val->get_type() != value::type::list) {
                throw std::runtime_error("APPENDS target is not a list");
            }
            auto &list_data = const_cast<list &>(list_val->as_list());
            std::ranges::move(items, std::back_inserter(list_data));
            break;
        }

        case opcodes::LIST: {
            auto items = pop_to_mark();
            stack_.push_back(value::list(std::move(items)));
            break;
        }

        case opcodes::EMPTY_TUPLE:
            stack_.push_back(value::tuple({}));
            break;

        case opcodes::TUPLE: {
            auto items = pop_to_mark();
            stack_.push_back(value::tuple(std::move(items)));
            break;
        }

        case opcodes::TUPLE1: {
            if (stack_.empty()) {
                throw std::runtime_error("Not enough items on stack for TUPLE1");
            }
            auto item = std::move(stack_.back());
            stack_.pop_back();
            list tuple_items;
            tuple_items.push_back(std::move(item));
            stack_.push_back(value::tuple(std::move(tuple_items)));
            break;
        }

        case opcodes::TUPLE2: {
            if (stack_.size() < 2) {
                throw std::runtime_error("Not enough items on stack for TUPLE2");
            }
            auto item2 = std::move(stack_.back());
            stack_.pop_back();
            auto item1 = std::move(stack_.back());
            stack_.pop_back();
            list tuple_items;
            tuple_items.push_back(std::move(item1));
            tuple_items.push_back(std::move(item2));
            stack_.push_back(value::tuple(std::move(tuple_items)));
            break;
        }

        case opcodes::TUPLE3: {
            if (stack_.size() < 3) {
                throw std::runtime_error("Not enough items on stack for TUPLE3");
            }
            auto item3 = std::move(stack_.back());
            stack_.pop_back();
            auto item2 = std::move(stack_.back());
            stack_.pop_back();
            auto item1 = std::move(stack_.back());
            stack_.pop_back();
            list tuple_items;
            tuple_items.push_back(std::move(item1));
            tuple_items.push_back(std::move(item2));
            tuple_items.push_back(std::move(item3));
            stack_.push_back(value::tuple(std::move(tuple_items)));
            break;
        }

        case opcodes::EMPTY_DICT:
            stack_.push_back(value::dict({}));
            break;

        case opcodes::DICT: {
            auto items = pop_to_mark();
            if (items.size() % 2 != 0) {
                throw std::runtime_error("Odd number of items for DICT");
            }
            dict dict_data;
            for (size_t i = 0; i < items.size(); i += 2) {
                if (items[i]->get_type() != value::type::string) {
                    throw std::runtime_error("Dict key must be string");
                }
                std::string key = items[i]->as_string();
                dict_data[std::move(key)] = std::move(items[i + 1]);
            }
            stack_.push_back(value::dict(std::move(dict_data)));
            break;
        }

        case opcodes::SETITEM: {
            if (stack_.size() < 3) {
                throw std::runtime_error("Not enough items on stack for SETITEM");
            }
            auto val = std::move(stack_.back());
            stack_.pop_back();
            auto key = std::move(stack_.back());
            stack_.pop_back();
            const auto &dict_val = stack_.back();

            if (dict_val->get_type() != value::type::dict) {
                throw std::runtime_error("SETITEM target is not a dict");
            }
            if (key->get_type() != value::type::string) {
                throw std::runtime_error("Dict key must be string");
            }

            auto &dict_data = const_cast<dict &>(dict_val->as_dict());
            dict_data[key->as_string()] = std::move(val);
            break;
        }

        case opcodes::SETITEMS: {
            auto items = pop_to_mark();
            if (items.size() % 2 != 0) {
                throw std::runtime_error("Odd number of items for SETITEMS");
            }
            if (stack_.empty()) {
                throw std::runtime_error("No dict on stack for SETITEMS");
            }
            const auto &dict_val = stack_.back();
            if (dict_val->get_type() != value::type::dict) {
                throw std::runtime_error("SETITEMS target is not a dict");
            }

            auto &dict_data = const_cast<dict &>(dict_val->as_dict());
            for (size_t i = 0; i < items.size(); i += 2) {
                if (items[i]->get_type() != value::type::string) {
                    throw std::runtime_error("Dict key must be string");
                }
                std::string key = items[i]->as_string();
                dict_data[std::move(key)] = std::move(items[i + 1]);
            }
            break;
        }

        case opcodes::BINPUT: {
            uint8_t memo_id = read_byte();
            if (stack_.empty()) {
                throw std::runtime_error("No item on stack for BINPUT");
            }
            memo_[memo_id] = clone(*stack_.back());
            break;
        }

        case opcodes::LONG_BINPUT: {
            uint32_t memo_id = read_uint32_le();
            if (stack_.empty()) {
                throw std::runtime_error("No item on stack for LONG_BINPUT");
            }
            memo_[memo_id] = clone(*stack_.back());
            break;
        }

        case opcodes::BINGET: {
            uint8_t memo_id = read_byte();
            const auto it = memo_.find(memo_id);
            if (it == memo_.end()) {
                throw std::runtime_error("Memo key not found");
            }
            stack_.push_back(clone(*it->second));
            break;
        }

        case opcodes::LONG_BINGET: {
            uint32_t memo_id = read_uint32_le();
            const auto it = memo_.find(memo_id);
            if (it == memo_.end()) {
                throw std::runtime_error("Memo key not found");
            }
            stack_.push_back(clone(*it->second));
            break;
        }

        case opcodes::PROTO: {
            read_byte();
            // Just ignore protocol version for now
            break;
        }

        case opcodes::FRAME: {
            read_uint64_le();
            // Just ignore frame size for now
            break;
        }

        case opcodes::LONG1: {
            uint8_t length = read_byte();
            if (length == 0) {
                stack_.push_back(value::int64(0));
            } else {
                if (length > sizeof(std::int64_t)) {
                    throw std::runtime_error("LONG1 value does not fit in int64");
                }

                const std::string bytes_data = read_string(length);
                auto bits = std::accumulate(bytes_data.rbegin(), bytes_data.rend(), std::uint64_t{0},
                                            [](const std::uint64_t current, const char byte) {
                                                return current << 8 | static_cast<std::uint8_t>(byte);
                                            });

                // Handle two's complement for negative numbers
                if (length < sizeof(bits) && (static_cast<std::uint8_t>(bytes_data.back()) & 0x80) != 0) {
                    bits |= ~std::uint64_t{0} << length * 8;
                }

                stack_.push_back(value::int64(std::bit_cast<std::int64_t>(bits)));
            }
            break;
        }

        case opcodes::MEMOIZE: {
            if (stack_.empty()) {
                throw std::runtime_error("No item on stack for MEMOIZE");
            }
            const auto memo_id = static_cast<unsigned int>(memo_.size());
            memo_[memo_id] = clone(*stack_.back());
            break;
        }

        default:
            throw std::runtime_error("Unsupported pickle opcode: " + std::to_string(opcode));
        }

        return nullptr; // Continue parsing
    }

    value_ptr parser::parse()
    {
        while (pos_ < data_.size()) {
            if (auto result = parse_value()) {
                return result;
            }
        }
        throw std::runtime_error("Unexpected end of pickle data");
    }

    value_ptr loads(const std::span<const std::byte> data)
    {
        parser p(data);
        return p.parse();
    }

    value_ptr loads(const std::string &data)
    {
        const auto byte_span = std::span(reinterpret_cast<const std::byte *>(data.data()), data.size());
        return loads(byte_span);
    }
} // namespace pickle
