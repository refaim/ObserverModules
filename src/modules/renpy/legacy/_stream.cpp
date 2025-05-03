// This is an open source non-commercial project. Dear PVS-Studio, please check it.
// PVS-Studio Static Code Analyzer for C, C++, C#, and Java: https://pvs-studio.com

#include "stdafx.h"

#include "_stream.h"

#include <cerrno>
#include <climits>
#include <cstdint>
#include <cstdlib>

#include <memory>
#include <string>

#include "modulecrt/Streams.h"

namespace kriabal::stream
{
    void FileStream::ReadBytes(std::string &buffer, size_t num_of_bytes)
    {
        if (buffer.size() < num_of_bytes)
            throw ReadError();

        if (!stream_->ReadBuffer(buffer.data(), num_of_bytes))
            throw ReadError();
    }

    int64_t FileStream::ReadSignedPositiveInt64FromHexString()
    {
        std::string buffer(sizeof(int64_t) * 2, '\0');
        ReadBytes(buffer, buffer.size());

        char *dummy;
        const int64_t result = std::strtoll(buffer.c_str(), &dummy, 16);
        if (errno == ERANGE) {
            if (result == LLONG_MIN)
                throw NumberReadOverflowError();
            if (result == LLONG_MAX)
                throw NumberReadUnderflowError();
        }

        if (result == 0)
            throw NumberReadNotANumberError();

        if (result < 0)
            throw NumberReadNotAPositiveNumberError();

        return result;
    }
}

