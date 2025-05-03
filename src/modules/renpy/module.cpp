#include "../extractor.h"

#include <fstream>
#include <zstr.hpp>

namespace extractor
{
    version_info get_version_info() noexcept
    {
        return {
            {0x9486718f, 0x8f0a, 0x4de7, {0x98, 0x80, 0x01, 0x14, 0x6b, 0x33, 0x6d, 0x6b}},
            3, 0,
        };
    }

    std::vector<std::byte> extractor::get_signature() noexcept
    {
        const std::string str = "RPA-3.0";
        std::vector<std::byte> signature(str.size());
        std::memcpy(signature.data(), str.data(), str.size());
        return signature;
    }

    // ReSharper disable once CppMemberFunctionMayBeStatic
    archive_info extractor::get_archive_info(const std::span<const std::byte> &data) noexcept // NOLINT(*-convert-member-functions-to-static)
    {
        return archive_info{L"RenPy", L"", L""};
    }

    int64_t read_int64(std::ifstream &stream)
    {
        std::string buffer(sizeof(int64_t) * 2, '\0');
        stream.read(buffer.data(), std::ssize(buffer));

        char *end_ptr = nullptr;
        const int64_t result = std::strtoll(buffer.c_str(), &end_ptr, 16);
        if (errno == ERANGE || result == 0 || result < 0) {
            throw std::out_of_range("NumberReadNotANumberError");
        }

        return result;
    }

    // ReSharper disable once CppMemberFunctionMayBeStatic
    std::vector<std::unique_ptr<file> > extractor::list_files(std::ifstream &stream) // NOLINT(*-convert-member-functions-to-static)
    {
        stream.seekg(std::string_view(" ").size(), std::ios::cur);

        const auto index_offset = read_int64(stream);
        const auto encryption_key = read_int64(stream);

        stream.seekg(index_offset);

        std::string decompressed_data;
        try {
            zstr::istream zs(stream);
            decompressed_data.assign(std::istreambuf_iterator(zs), std::istreambuf_iterator<char>());
        } catch (const std::ios_base::failure &) {
            throw read_error();
        }

        auto files = std::vector<std::unique_ptr<file> >();
        // TODO: Разобрать decompressed_data и заполнить files
        return files;
    }
}
