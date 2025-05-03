#ifndef EXTRACTOR_H
#define EXTRACTOR_H

#include <memory>
#include <span>
#include <stdexcept>
#include <string>
#include <vector>

namespace extractor
{
    typedef struct
    {
        uint32_t Data1;
        uint16_t Data2;
        uint16_t Data3;
        uint8_t Data4[8];
    } guid;

    typedef struct
    {
        guid id;
        uint32_t major_version;
        uint32_t minor_version;
    } version_info;

    version_info get_version_info() noexcept;

    class read_error final : public std::runtime_error
    {
    public:
        read_error(): runtime_error("")
        {
        }
    };

    struct archive_info final
    {
        std::wstring format;
        std::wstring compression;
        std::wstring comment;
    };

    struct file final
    {
        std::string path;
        int64_t offset;
        int64_t compressed_size_in_bytes;
        int64_t uncompressed_size_in_bytes;
    };

    class extractor
    {
    public:
        static std::vector<std::byte> get_signature() noexcept;

        archive_info get_archive_info(const std::span<const std::byte> &data) noexcept;

        std::vector<std::unique_ptr<file> > list_files(std::ifstream &stream);
    };
}

#endif
