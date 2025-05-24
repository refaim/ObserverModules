#include "../extractor.h"
#include "pickle.h"

#include <fstream>
#include <functional>

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
        const std::string str = "RPA-";
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

    std::pair<int64_t, std::function<std::pair<int64_t, int64_t>(int64_t, int64_t)> > parse_header(
        std::ifstream &stream)
    {
        std::string version_check(3, '\0');
        stream.seekg(static_cast<std::streamoff>(extractor::get_signature().size()));
        stream.read(version_check.data(), 3);

        if (version_check == "2.0") {
            stream.seekg(static_cast<std::streamoff>(std::string("RPA-2.0 ").length()));
            const auto index_offset = read_int64(stream);
            return {
                index_offset, [](int64_t offset, int64_t length)
                {
                    return std::make_pair(offset, length);
                }
            };
        }

        if (version_check == "3.0") {
            stream.seekg(static_cast<std::streamoff>(std::string("RPA-3.0 ").length()));
            const auto index_offset = read_int64(stream);
            const auto encryption_key = read_int64(stream);
            return {
                index_offset, [encryption_key](int64_t offset, int64_t length)
                {
                    return std::make_pair(offset ^ encryption_key, length ^ encryption_key);
                }
            };
        }

        throw std::runtime_error("Unsupported RPA version");
    }

    // ReSharper disable once CppMemberFunctionMayBeStatic
    std::vector<std::unique_ptr<file> > extractor::list_files(std::ifstream &stream) // NOLINT(*-convert-member-functions-to-static)
    {
        const auto [index_offset, decoder] = parse_header(stream);

        stream.seekg(index_offset);

        std::string decompressed_data;
        try {
            zstr::istream zs(stream);
            decompressed_data.assign(std::istreambuf_iterator(zs), std::istreambuf_iterator<char>());
        } catch (const std::ios_base::failure &) {
            throw read_error();
        }

        auto root = pickle::loads(decompressed_data);
        const auto &dict = root->as_dict();
        auto files = std::vector<std::unique_ptr<file> >();
        files.reserve(dict.size());

        for (const auto &[file_name, value]: dict) {
            const auto &props_container = value->as_list();
            if (props_container.size() != 1) {
                throw std::logic_error("Not implemented");
            }

            const auto &props = props_container[0]->as_tuple();
            if (props.size() < 2) {
                throw std::logic_error("Expected at least 2 elements in tuple");
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
        return magic;
    }
}
