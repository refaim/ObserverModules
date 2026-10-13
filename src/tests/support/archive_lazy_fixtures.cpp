#include "archive_lazy_fixtures.h"

#include <atomic>
#include <format>
#include <fstream>
#include <ios>
#include <memory>
#include <span>
#include <stdexcept>
#include <system_error>
#include <utility>

#include <windows.h>

namespace
{
    test::support::fake_extractor_state *g_state = nullptr;
    test::support::fake_extractor_state g_default_state;

    test::support::fake_extractor_state &fake_state()
    {
        return g_state != nullptr ? *g_state : g_default_state;
    }
} // namespace

// Test-only definitions of the per-DLL extractor functions. The unit test binary
// loads the real modules as DLLs, so these never collide with production symbols.
namespace extractor
{
    std::vector<std::byte> extractor::get_signature()
    {
        return fake_state().signature;
    }

    archive_info extractor::get_archive_info(std::istream &stream, const std::span<const std::byte> &data,
                                             const std::filesystem::path &path, const std::string_view password)
    {
        auto &state = fake_state();
        ++state.archive_info_calls;
        if (state.storage_factory) {
            state_ = state.storage_factory();
        }
        state.archive_info_data_size = data.size();
        state.archive_info_path = path;
        state.archive_info_password = password;
        stream.clear();
        stream.seekg(0, std::ios::end);
        state.archive_info_stream_size = static_cast<std::streamoff>(stream.tellg());
        stream.seekg(0);
        return state.archive_info;
    }

    std::vector<std::unique_ptr<file>> extractor::list_files(std::istream &stream)
    {
        static_cast<void>(stream);
        auto &state = fake_state();
        ++state.list_files_calls;
        if (state.list_files_read_error) {
            throw ::extractor::read_error();
        }

        std::vector<std::unique_ptr<file>> files;
        files.reserve(state.files.size());
        for (const auto &definition : state.files) {
            auto item = std::make_unique<file>();
            item->path = definition.path;
            item->header = definition.header;
            item->offset = definition.offset;
            item->compressed_body_size_in_bytes = definition.compressed_body_size_in_bytes;
            item->uncompressed_body_size_in_bytes = definition.uncompressed_body_size_in_bytes;
            item->magic = definition.magic;
            item->extract = definition.extract;
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

namespace test::support
{
    fake_extractor_scope::fake_extractor_scope(fake_extractor_state &state) noexcept : previous_(g_state)
    {
        g_state = &state;
    }

    fake_extractor_scope::~fake_extractor_scope()
    {
        g_state = previous_;
    }

    temporary_file::temporary_file(const std::string_view label, const std::span<const std::uint8_t> contents)
        : path_(unused_path(label))
    {

        std::ofstream output(path_, std::ios::binary | std::ios::trunc);
        if (!output.is_open()) {
            throw std::runtime_error(std::format("Failed to create lazy fixture {}", path_.string()));
        }
        output.write(reinterpret_cast<const char *>(contents.data()), static_cast<std::streamsize>(contents.size()));
        output.close();
        if (!output) {
            std::error_code error;
            std::filesystem::remove(path_, error);
            throw std::runtime_error(std::format("Failed to write lazy fixture {}", path_.string()));
        }
    }

    temporary_file::~temporary_file()
    {
        std::error_code error;
        std::filesystem::remove(path_, error);
    }

    const std::filesystem::path &temporary_file::path() const noexcept
    {
        return path_;
    }

    temporary_directory::temporary_directory(const std::string_view label) : path_(unused_path(label))
    {
        std::error_code error;
        if (!std::filesystem::create_directory(path_, error) || error) {
            throw std::runtime_error(std::format("Failed to create lazy directory {}", path_.string()));
        }
    }

    temporary_directory::~temporary_directory()
    {
        std::error_code error;
        std::filesystem::remove_all(path_, error);
    }

    const std::filesystem::path &temporary_directory::path() const noexcept
    {
        return path_;
    }

    std::filesystem::path unused_path(const std::string_view label)
    {
        static std::atomic_uint32_t sequence = 0;
        // The process id keeps concurrent test processes from clobbering each other's
        // fixtures; the per-process counter keeps calls within one process distinct.
        return std::filesystem::temp_directory_path() /
               std::format("unpackrat-lazy-{}-{}-{}.tmp", label, GetCurrentProcessId(), sequence.fetch_add(1));
    }
} // namespace test::support
