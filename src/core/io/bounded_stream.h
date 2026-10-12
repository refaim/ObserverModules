#pragma once

#include <cstddef>
#include <cstdint>
#include <istream>
#include <stdexcept>
#include <type_traits>

namespace observer::io
{
    class read_error final : public std::runtime_error
    {
      public:
        read_error() : std::runtime_error("bounded stream read failed")
        {
        }
    };

    class bounded_stream final
    {
      public:
        explicit bounded_stream(std::istream &stream);

        [[nodiscard]] std::streamoff size() const noexcept;
        [[nodiscard]] std::streamoff position();
        [[nodiscard]] std::streamoff remaining();
        void seek_absolute(std::streamoff offset);
        void read_exact(char *destination, std::size_t byte_count);

        template <typename Value>
            requires std::is_trivially_copyable_v<Value>
        [[nodiscard]] Value read_trivial()
        {
            Value value{};
            read_exact(reinterpret_cast<char *>(&value), sizeof(value));
            return value;
        }

      private:
        std::istream &stream_;
        std::streamoff size_ = 0;
    };
} // namespace observer::io
