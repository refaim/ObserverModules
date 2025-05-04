#include "archive.h"
#include "modules/extractor.h"

#include <array>
#include <filesystem>
#include <fstream>
#include <span>
#include <stdexcept>

namespace archive
{
    archive::archive(std::unique_ptr<extractor::extractor> extractor)
    {
        extractor_ = std::move(extractor);
    }

    bool starts_with_bytes(std::span<const std::byte> data, std::span<const std::byte> signature) noexcept
    {
        if (signature.size() > data.size()) {
            return false;
        }
        return std::equal(signature.begin(), signature.end(), data.begin());
    }

    extractor::archive_info archive::open(const std::filesystem::path &path, const std::span<const std::byte> &data)
    {
        auto signature = extractor::extractor::get_signature();
        if (!data.empty() && !starts_with_bytes(data, signature)) {
            throw std::runtime_error("Signature mismatch");
        }

        stream_ = std::make_unique<std::ifstream>(path, std::ios::binary);
        if (!stream_->is_open()) {
            throw std::runtime_error("Failed to open file");
        }
        stream_->exceptions(std::ifstream::failbit | std::ifstream::badbit);
        stream_->seekg(std::ssize(signature));

        return extractor_->get_archive_info(data);
    }

    void archive::prepare_files()
    {
        if (!files_.empty()) {
            return;
        }

        try {
            files_ = extractor_->list_files(*stream_);
        } catch (extractor::read_error &) {
            throw read_error();
        }

        for (const auto &file: files_) {
            std::ranges::replace(file->path, '/', '\\');
        }
    }

    const extractor::file &archive::get_file(const size_t index) const
    {
        if (index >= files_.size()) {
            throw std::out_of_range("Index out of range");
        }
        return *files_[index];
    }

    void archive::extract_file(const size_t index, const std::filesystem::path &path,
                               const std::function<void(int64_t)> &report_progress) const
    {
        const extractor::file &file = get_file(index);
        std::ofstream output(path, std::ios::binary);
        if (!output.is_open()) {
            throw write_error();
        }
        output.exceptions(std::ofstream::failbit | std::ofstream::badbit);

        constexpr int64_t buffer_size = 128 * 1024;
        std::vector<char> buffer(buffer_size);

        if (!file.header.empty()) {
            output.write(file.header.data(), std::ssize(file.header));
        }

        try {
            stream_->seekg(file.offset);
        } catch (std::ios_base::failure &) {
            throw read_error();
        }

        int64_t bytes_left = file.compressed_body_size_in_bytes;
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
