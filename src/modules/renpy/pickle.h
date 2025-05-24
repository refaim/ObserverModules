#pragma once

#include <vector>
#include <unordered_map>
#include <variant>
#include <string>
#include <memory>
#include <span>
#include <stdexcept>

namespace pickle
{
    class value;

    using value_ptr = std::unique_ptr<value>;
    using list = std::vector<value_ptr>;
    using dict = std::unordered_map<std::string, value_ptr>;

    class value
    {
    public:
        enum class type
        {
            none,
            bool_,
            int64,
            float64,
            bytes,
            string,
            list,
            dict,
            tuple
        };

    private:
        type type_;
        std::variant<
            std::monostate, // none
            bool, // bool_
            int64_t, // int64
            double, // float64
            std::string, // bytes/string
            list, // list/tuple
            dict // dict
        > data_;

    public:
        explicit value(const type t) : type_(t)
        {
        }

        static value_ptr none()
        {
            return std::make_unique<value>(type::none);
        }

        static value_ptr boolean(bool val)
        {
            auto v = std::make_unique<value>(type::bool_);
            v->data_ = val;
            return v;
        }

        static value_ptr int64(int64_t val)
        {
            auto v = std::make_unique<value>(type::int64);
            v->data_ = val;
            return v;
        }

        static value_ptr float64(double val)
        {
            auto v = std::make_unique<value>(type::float64);
            v->data_ = val;
            return v;
        }

        static value_ptr bytes(std::string val)
        {
            auto v = std::make_unique<value>(type::bytes);
            v->data_ = std::move(val);
            return v;
        }

        static value_ptr string(std::string val)
        {
            auto v = std::make_unique<value>(type::string);
            v->data_ = std::move(val);
            return v;
        }

        static value_ptr list(pickle::list val)
        {
            auto v = std::make_unique<value>(type::list);
            v->data_ = std::move(val);
            return v;
        }

        static value_ptr tuple(pickle::list val)
        {
            auto v = std::make_unique<value>(type::tuple);
            v->data_ = std::move(val);
            return v;
        }

        static value_ptr dict(pickle::dict val)
        {
            auto v = std::make_unique<value>(type::dict);
            v->data_ = std::move(val);
            return v;
        }

        type get_type() const { return type_; }

        bool as_bool() const
        {
            if (type_ != type::bool_) {
                throw std::runtime_error("Value is not a bool");
            }
            return std::get<bool>(data_);
        }

        int64_t as_int64() const
        {
            if (type_ != type::int64) {
                throw std::runtime_error("Value is not an int64");
            }
            return std::get<int64_t>(data_);
        }

        double as_float64() const
        {
            if (type_ != type::float64) {
                throw std::runtime_error("Value is not a float64");
            }
            return std::get<double>(data_);
        }

        const std::string &as_string() const
        {
            if (type_ != type::string && type_ != type::bytes) {
                throw std::runtime_error("Value is not a string");
            }
            return std::get<std::string>(data_);
        }

        const pickle::list &as_list() const
        {
            if (type_ != type::list) {
                throw std::runtime_error("Value is not a list");
            }
            return std::get<pickle::list>(data_);
        }

        const pickle::list &as_tuple() const
        {
            if (type_ != type::tuple) {
                throw std::runtime_error("Value is not a tuple");
            }
            return std::get<pickle::list>(data_);
        }

        const pickle::dict &as_dict() const
        {
            if (type_ != type::dict) {
                throw std::runtime_error("Value is not a dict");
            }
            return std::get<pickle::dict>(data_);
        }
    };

    class parser
    {
    private:
        std::span<const std::byte> data_;
        size_t pos_ = 0;
        std::vector<value_ptr> stack_;
        std::vector<size_t> mark_stack_;
        std::unordered_map<uint32_t, value_ptr> memo_;

        uint8_t read_byte();

        uint16_t read_uint16_le();

        uint32_t read_uint32_le();

        uint64_t read_uint64_le();

        std::string read_string(size_t length);

        std::string read_line();

        void push_mark();

        list pop_to_mark();

        value_ptr parse_value();

    public:
        explicit parser(const std::span<const std::byte> data) : data_(data)
        {
        }

        value_ptr parse();
    };

    value_ptr loads(std::span<const std::byte> data);

    value_ptr loads(const std::string &data);
}
