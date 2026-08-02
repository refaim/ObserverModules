#include "bounded_stream.h"

#include <limits>

namespace observer::io
{
    bounded_stream::bounded_stream(std::istream &stream) : stream_(stream)
    {
        try {
            const auto original = stream_.tellg();
            stream_.seekg(0, std::ios::end);
            const auto end = stream_.tellg();
            stream_.seekg(original);
            if (original < 0 || end < original) {
                throw read_error();
            }
            size_ = static_cast<std::streamoff>(end);
        } catch (const std::ios_base::failure &) {
            throw read_error();
        }
    }

    std::streamoff bounded_stream::size() const noexcept
    {
        return size_;
    }

    std::streamoff bounded_stream::position()
    {
        try {
            const auto result = stream_.tellg();
            if (result < 0 || result > size_) {
                throw read_error();
            }
            return static_cast<std::streamoff>(result);
        } catch (const std::ios_base::failure &) {
            throw read_error();
        }
    }

    std::streamoff bounded_stream::remaining()
    {
        return size_ - position();
    }

    void bounded_stream::seek_absolute(const std::streamoff offset)
    {
        if (offset < 0 || offset > size_) {
            throw read_error();
        }
        try {
            stream_.seekg(offset);
        } catch (const std::ios_base::failure &) {
            throw read_error();
        }
        if (!stream_) {
            throw read_error();
        }
    }

    void bounded_stream::read_exact(char *destination, const std::size_t byte_count)
    {
        if (byte_count > static_cast<std::size_t>(std::numeric_limits<std::streamsize>::max()) ||
            byte_count > static_cast<std::uint64_t>(remaining())) {
            throw read_error();
        }
        const auto stream_size = static_cast<std::streamsize>(byte_count);
        try {
            stream_.read(destination, stream_size);
        } catch (const std::ios_base::failure &) {
            throw read_error();
        }
        if (stream_.gcount() != stream_size) {
            throw read_error();
        }
    }
} // namespace observer::io
