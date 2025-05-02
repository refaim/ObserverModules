#ifndef RENPY_H
#define RENPY_H

#include <filesystem>
#include <fstream>
#include <functional>
#include <span>
#include <string>

namespace shared
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

    struct meta final
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

    class archive final
    {
    public:
        ~archive();

        meta open(const std::filesystem::path &path, const std::span<const std::byte> &data);

        void prepare_files();

        [[nodiscard]] const file &get_file(size_t index) const;

        void extract_file(size_t index, const std::filesystem::path &path,
                          const std::function<void(int64_t)> &report_progress) const;

    private:
        std::unique_ptr<std::ifstream> stream_;
        std::vector<std::unique_ptr<file> > files_;
    };
}

#endif
