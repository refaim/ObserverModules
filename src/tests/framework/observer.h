#pragma once

#include <filesystem>
#include <functional>

namespace test
{
    struct file
    {
        const std::wstring path;
        const int64_t uncompressed_size;
        const int64_t compressed_size;
        const std::function<void(const std::filesystem::path &)> extract;
    };

    class module
    {
    public:
        module()
        {
        };

        virtual ~module() = default;
    };

    class observer final
    {
    public:
        observer();

        std::vector<file> list_files(const std::filesystem::path &path) const;

    private:
        std::vector<std::unique_ptr<module> > modules_;
    };
}
