#include "../../core/compression/zlib_codec.h"
#include "../../core/io/bounded_stream.h"
#include "../extractor.h"
#include "pickle.h"

#include <fstream>
#include <functional>
#include <limits>

namespace extractor
{
    version_info get_version_info() noexcept
    {
        return {
            {0x9486718f, 0x8f0a, 0x4de7, {0x98, 0x80, 0x01, 0x14, 0x6b, 0x33, 0x6d, 0x6b}},
            3,
            0,
        };
    }

    std::vector<std::byte> extractor::get_signature()
    {
        const std::string str = "RPA-";
        std::vector<std::byte> signature(str.size());
        std::memcpy(signature.data(), str.data(), str.size());
        return signature;
    }

    archive_info extractor::get_archive_info(
        const std::span<const std::byte> &data) // NOLINT(*-convert-member-functions-to-static)
    {
        static_cast<void>(data);
        return archive_info{L"RenPy", L"", L""};
    }

    int64_t read_int64(observer::io::bounded_stream &stream)
    {
        std::string buffer(sizeof(int64_t) * 2, '\0');
        stream.read_exact(buffer.data(), buffer.size());

        char *end_ptr = nullptr;
        errno = 0;
        const int64_t result = std::strtoll(buffer.c_str(), &end_ptr, 16);
        if (errno == ERANGE || result == 0 || result < 0) {
            throw std::out_of_range("NumberReadNotANumberError");
        }

        return result;
    }

    std::pair<int64_t, std::function<std::pair<int64_t, int64_t>(int64_t, int64_t)>> parse_header(
        observer::io::bounded_stream &stream)
    {
        std::string version_check(3, '\0');
        stream.seek_absolute(static_cast<std::streamoff>(extractor::get_signature().size()));
        stream.read_exact(version_check.data(), version_check.size());

        if (version_check == "2.0") {
            stream.seek_absolute(static_cast<std::streamoff>(std::string("RPA-2.0 ").length()));
            const auto index_offset = read_int64(stream);
            return {index_offset, [](int64_t offset, int64_t length) { return std::make_pair(offset, length); }};
        }

        if (version_check == "3.0") {
            stream.seek_absolute(static_cast<std::streamoff>(std::string("RPA-3.0 ").length()));
            const auto index_offset = read_int64(stream);
            const auto encryption_key = read_int64(stream);
            return {index_offset, [encryption_key](int64_t offset, int64_t length) {
                        return std::make_pair(offset ^ encryption_key, length ^ encryption_key);
                    }};
        }

        throw std::runtime_error("Unsupported RPA version");
    }

    std::vector<std::unique_ptr<file>> extractor::list_files(
        std::istream &stream) // NOLINT(*-convert-member-functions-to-static)
    {
        observer::io::bounded_stream input(stream);
        const auto [index_offset, decoder] = parse_header(input);
        const auto archive_size = input.size();
        if (index_offset >= archive_size) {
            throw read_error();
        }
        input.seek_absolute(index_offset);

        constexpr std::size_t max_compressed_index_size = 64ULL * 1024 * 1024;
        constexpr std::size_t max_decompressed_index_size = 64ULL * 1024 * 1024;
        const auto compressed_size = archive_size - index_offset;
        if (static_cast<std::uint64_t>(compressed_size) > max_compressed_index_size) {
            throw std::runtime_error("RPA compressed index exceeds the metadata budget");
        }

        std::vector<std::byte> compressed_data(static_cast<std::size_t>(compressed_size));
        input.read_exact(reinterpret_cast<char *>(compressed_data.data()), compressed_data.size());
        const auto decompressed_data =
            observer::compression::decompress_zlib(compressed_data, max_decompressed_index_size);

        auto root = pickle::loads(decompressed_data);
        const auto &dict = root->as_dict();
        auto files = std::vector<std::unique_ptr<file>>();
        files.reserve(dict.size());

        for (const auto &[file_name, value] : dict) {
            const auto &props_container = value->as_list();
            if (props_container.size() != 1) {
                throw std::runtime_error("Expected exactly one property tuple");
            }

            const auto &props = props_container[0]->as_tuple();
            if (props.size() < 2) {
                throw std::runtime_error("Expected at least 2 elements in tuple");
            }

            const auto [offset, body_size] = decoder(props[0]->as_int64(), props[1]->as_int64());

            auto header = std::string();
            if (props.size() >= 3) {
                if (props[2]->get_type() != pickle::value::type::none) {
                    header = props[2]->as_string();
                }
            }

            auto item = std::make_unique<file>();
            item->path = file_name;
            item->header = header;
            item->offset = offset;
            item->compressed_body_size_in_bytes = body_size - static_cast<int64_t>(header.size());
            item->uncompressed_body_size_in_bytes = item->compressed_body_size_in_bytes;
            files.push_back(std::move(item));
        }
        return files;
    }

    uint32_t extractor::decrypt(uint32_t magic, std::vector<char> &data) const
    {
        static_cast<void>(data);
        return magic;
    }
} // namespace extractor
