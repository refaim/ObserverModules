#include <array>
#include <filesystem>
#include <fstream>
#include <span>
#include <stdexcept>
#include <string>

#include "archive.h"

namespace shared
{
    bool starts_with_bytes(std::span<const std::byte> data, std::span<const std::byte> signature) noexcept
    {
        if (signature.size() > data.size()) {
            return false;
        }
        return std::equal(signature.begin(), signature.end(), data.begin());
    }

    meta archive::open(const std::filesystem::path &path, const std::span<const std::byte> &data)
    {
        constexpr std::array signature = {std::byte{0}, std::byte{0}, std::byte{0}, std::byte{0}};

        if (!data.empty() && !starts_with_bytes(data, signature)) {
            throw std::runtime_error("Signature mismatch");
        }

        stream_ = std::make_unique<std::ifstream>(path, std::ios::binary);
        if (!stream_->is_open()) {
            throw std::runtime_error("Failed to open file");
        }
        stream_->exceptions(std::ifstream::failbit | std::ifstream::badbit);
        stream_->seekg(signature.size());

        return meta{L"Zanzarah", L"", L""};
    }

    archive::~archive()
    {
        if (stream_ && stream_->is_open()) {
            stream_->close();
        }
        stream_.reset();
    }

    int32_t read_positive_or_zero_int32(std::ifstream &stream)
    {
        int32_t value;

        try {
            stream.read(reinterpret_cast<char *>(&value), sizeof(value));
        } catch (std::ios_base::failure &) {
            throw read_error();
        }

        if (value < 0) {
            throw read_error();
        }

        return value;
    }

    int32_t read_positive_int32(std::ifstream &stream)
    {
        const int32_t value = read_positive_or_zero_int32(stream);
        if (value == 0) {
            throw read_error();
        }
        return value;
    }

    void normalize_path(std::string &path)
    {
        if (constexpr std::string_view relative_prefix = "..\\"; path.starts_with(relative_prefix)) {
            path.erase(0, relative_prefix.size());
        }

        std::ranges::replace(path, '/', '\\');
    }

    void archive::prepare_files()
    {
        if (!files_.empty()) {
            return;
        }

        const size_t files_num = read_positive_int32(*stream_);
        files_.reserve(files_num);

        std::string path;
        for (size_t i = 0; i < files_num; ++i) {
            const auto path_len = read_positive_int32(*stream_);
            path.resize(path_len);
            stream_->read(path.data(), path_len);

            const auto block_offset = read_positive_or_zero_int32(*stream_);
            const auto block_size = read_positive_int32(*stream_);
            [[maybe_unused]] constexpr int32_t data_block_footer = 0x202;
            [[maybe_unused]] constexpr int32_t data_block_header = 0x101;

            auto new_file = std::make_unique<file>();
            new_file->path = path;
            new_file->offset = block_offset + static_cast<int>(sizeof(data_block_header));
            // [0x101, data, 0x202], [0x101, data, 0x202], ...
            // No idea why we should subtract next data block header, but apparently that's the right way
            new_file->compressed_size_in_bytes =
                    block_size - 2 * static_cast<int>(sizeof(data_block_header)) - static_cast<int>(sizeof(
                        data_block_footer));
            new_file->uncompressed_size_in_bytes = new_file->compressed_size_in_bytes;

            files_.push_back(std::move(new_file));
        }

        for (const auto &file_ptr: files_) {
            file_ptr->offset += stream_->tellg();
            normalize_path(file_ptr->path);
        }
    }

    const file &archive::get_file(const size_t index) const
    {
        if (index >= files_.size()) {
            throw std::out_of_range("Index out of range");
        }
        return *files_[index];
    }

    void archive::extract_file(const size_t index, const std::filesystem::path &path,
                               const std::function<void(int64_t)> &report_progress) const
    {
        const file &item = get_file(index);
        std::ofstream output(path, std::ios::binary);
        if (!output.is_open()) {
            throw write_error();
        }
        output.exceptions(std::ofstream::failbit | std::ofstream::badbit);

        constexpr int64_t buffer_size = 128 * 1024;
        std::vector<char> buffer(buffer_size);

        try {
            stream_->seekg(item.offset);
        } catch (std::ios_base::failure &) {
            throw read_error();
        }

        int64_t bytes_left = item.compressed_size_in_bytes;
        while (bytes_left > 0) {
            const auto chunk_size = static_cast<std::streamsize>(std::min(bytes_left, buffer_size));

            try {
                stream_->read(buffer.data(), chunk_size);
            } catch (std::ios_base::failure &) {
                throw read_error();
            }

            try {
                output.write(buffer.data(), chunk_size);
            } catch (std::ios_base::failure &) {
                throw write_error();
            }

            bytes_left -= chunk_size;

            try {
                report_progress(chunk_size);
            } catch (user_interrupt &) {
                return;
            }
        }
    }
}
