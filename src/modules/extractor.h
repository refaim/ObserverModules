#pragma once

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
        std::string header;
        int64_t offset;
        int64_t compressed_body_size_in_bytes;
        int64_t uncompressed_body_size_in_bytes;
        uint32_t magic = 0;
    };

    class extractor
    {
    public:
        virtual ~extractor() = default;

        static std::vector<std::byte> get_signature() noexcept;

        archive_info get_archive_info(const std::span<const std::byte> &data) noexcept;

        std::vector<std::unique_ptr<file> > list_files(std::ifstream &stream);

        uint32_t decrypt(uint32_t magic, std::vector<char> &data) const;
    };
}
