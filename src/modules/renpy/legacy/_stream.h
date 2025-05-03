#ifndef KRIABAL_STREAM_H_
#define KRIABAL_STREAM_H_

#include <cstdint>

#include <memory>
#include <string>

#include "ModuleWin.h"
#include "modulecrt/Streams.h"

#include "_common.h"

namespace kriabal::stream
{
    class RuntimeError : public kriabal::RuntimeError {};

    class FileOpenError : public RuntimeError {};
    class ReadError : public RuntimeError {};
    class WriteError : public RuntimeError {};

    class NumberReadError : public RuntimeError {};
    class NumberReadNotANumberError : public RuntimeError {};
    class NumberReadNotAPositiveNumberError : public RuntimeError {};
    class NumberReadNotAPositiveOrZeroNumberError : public RuntimeError {};
    class NumberReadOverflowError : public RuntimeError {};
    class NumberReadUnderflowError : public RuntimeError {};

    class FileStream
    {
    public:
        FileStream(std::wstring path, bool read_only, bool create_if_not_exists);
        int64_t GetPosition();
        int64_t GetFileSizeInBytes();
        void Seek(int64_t position);
        void Skip(int64_t num_of_bytes);
        void ReadBytes(std::string& buffer, size_t num_of_bytes);
        void WriteBytes(const std::string& buffer, size_t num_of_bytes);
        int32_t ReadSignedPositiveInt32FromBytes();
        int32_t ReadSignedPositiveOrZeroInt32FromBytes();
        int64_t ReadSignedPositiveInt64FromHexString();
    private:
        std::unique_ptr<CFileStream> stream_;
    };
}

#endif // KRIABAL_STREAM_H_
