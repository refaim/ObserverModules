#pragma once

#include "modules/extractor.h"

#include <filesystem>
#include <fstream>
#include <functional>
#include <span>

namespace archive
{
    class read_error final : public std::runtime_error
    {
    public:
        read_error(): runtime_error("")
        {
        }
    };

    class write_error final : public std::runtime_error
    {
    public:
        write_error(): runtime_error("")
        {
        }
    };

    class user_interrupt final : public std::runtime_error
    {
    public:
        user_interrupt(): runtime_error("")
        {
        }
    };

    class archive final
    {
    public:
        explicit archive(std::unique_ptr<extractor::extractor> extractor);

        extractor::archive_info open(const std::filesystem::path &path, const std::span<const std::byte> &data);

        void prepare_files();

        [[nodiscard]] const extractor::file &get_file(size_t index) const;

        void extract_file(size_t index, const std::filesystem::path &path,
                          const std::function<void(int64_t)> &report_progress) const;

    private:
        std::unique_ptr<extractor::extractor> extractor_;
        std::unique_ptr<std::ifstream> stream_;
        std::vector<std::unique_ptr<extractor::file> > files_;
    };
}
