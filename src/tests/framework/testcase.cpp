#include "testcase.h"

#include "observer.h"

#include <fstream>

#include <catch2/catch_test_macros.hpp>
#include <nlohmann/json.hpp>
#include <windows.h>
#include <xxhash.h>

namespace test
{
    class temporary_file final
    {
      public:
        explicit temporary_file(std::filesystem::path path) : path_(std::move(path))
        {
        }

        ~temporary_file()
        {
            std::error_code error;
            std::filesystem::remove(path_, error);
        }

        [[nodiscard]] const std::filesystem::path &path() const noexcept
        {
            return path_;
        }

      private:
        std::filesystem::path path_;
    };

    std::string wide_to_utf8(const std::wstring &wide)
    {
        const auto wide_size = static_cast<int>(wide.size());
        const auto utf8_size = WideCharToMultiByte(CP_UTF8, 0, wide.data(), wide_size, nullptr, 0, nullptr, nullptr);
        REQUIRE(utf8_size > 0);
        std::string utf8(utf8_size, 0);
        REQUIRE(WideCharToMultiByte(CP_UTF8, 0, wide.data(), wide_size, utf8.data(), utf8_size, nullptr, nullptr) > 0);
        return utf8;
    }

    std::string hash_to_string(const XXH64_hash_t &hash)
    {
        std::stringstream ss;
        ss << std::hex << std::setw(16) << std::setfill('0') << hash;
        return ss.str();
    }

    std::filesystem::path make_unique_path(const std::filesystem::path &archive_path, const file &file)
    {
        const std::string buffer = wide_to_utf8(archive_path) + wide_to_utf8(file.path);
        const auto name = hash_to_string(XXH3_64bits(buffer.data(), buffer.size())) + ".tmp";
        return std::filesystem::temp_directory_path() / name;
    }

    std::string hash_file(const std::filesystem::path &path)
    {
        std::ifstream file(path, std::ios::binary);
        REQUIRE(file.is_open());

        std::string buffer;
        buffer.resize(128ULL * 1024);

        auto state = XXH3_createState();
        REQUIRE(state != nullptr);
        REQUIRE(XXH3_64bits_reset(state) == XXH_OK);

        while (file.good()) {
            file.read(buffer.data(), static_cast<std::streamsize>(buffer.size()));
            REQUIRE(XXH3_64bits_update(state, buffer.data(), static_cast<size_t>(file.gcount())) == XXH_OK);
        }

        const auto hash = XXH3_64bits_digest(state);
        REQUIRE(XXH3_freeState(state) == XXH_OK);
        return hash_to_string(hash);
    }

    std::string read_file(const std::filesystem::path &path)
    {
        std::ifstream input(path, std::ios::binary);
        REQUIRE(input.is_open());
        return {std::istreambuf_iterator(input), std::istreambuf_iterator<char>()};
    }

    void test_archive(const std::filesystem::path &path, const std::vector<expected_file> &expected_files)
    {
        const auto plugin = observer();
        const auto files = plugin.list_files(path);
        REQUIRE(files.size() == expected_files.size());

        for (const auto &file : files) {
            const auto expected = std::ranges::find(expected_files, file.path, &expected_file::path);
            REQUIRE(expected != expected_files.end());

            temporary_file extracted(make_unique_path(path, file));
            file.extract(extracted.path());
            REQUIRE(file.uncompressed_size >= 0);
            REQUIRE(std::filesystem::file_size(extracted.path()) ==
                    static_cast<std::uintmax_t>(file.uncompressed_size));
            REQUIRE(read_file(extracted.path()) == expected->contents);
        }
    }

    void test_external_archive(const std::filesystem::path &path)
    {
        const auto required_size = GetEnvironmentVariableW(L"OBSERVER_TEST_CORPUS", nullptr, 0);
        if (required_size == 0) {
            SKIP("OBSERVER_TEST_CORPUS is not set; external golden corpus test skipped");
        }

        std::wstring corpus_root(required_size, L'\0');
        const auto written = GetEnvironmentVariableW(L"OBSERVER_TEST_CORPUS", corpus_root.data(), required_size);
        REQUIRE(written > 0);
        REQUIRE(written < required_size);
        corpus_root.resize(written);

        const auto archive_path = std::filesystem::path(corpus_root) / path;
        const auto folder = archive_path.parent_path();

        std::ifstream expected_listing(folder / std::format(L"{}.expected.json", archive_path.stem().wstring()));
        REQUIRE(expected_listing.is_open());
        auto listing_json = nlohmann::json::parse(expected_listing);
        auto expected_strings = listing_json.get<std::vector<std::string>>();
        std::ranges::sort(expected_strings);

        const auto plugin = observer();
        std::vector<std::string> actual_strings;
        for (const auto files = plugin.list_files(archive_path); const auto &file : files) {
            temporary_file extracted(make_unique_path(archive_path, file));
            file.extract(extracted.path());
            const auto file_size = std::filesystem::file_size(extracted.path());
            REQUIRE(file.uncompressed_size >= 0);
            REQUIRE(file_size == static_cast<decltype(file_size)>(file.uncompressed_size));
            actual_strings.emplace_back(
                std::format("{} {} {}", wide_to_utf8(file.path), file_size, hash_file(extracted.path())));
        }
        std::ranges::sort(actual_strings);

        REQUIRE(expected_strings.size() == actual_strings.size());
        for (size_t i = 0; i < expected_strings.size(); i++) {
            REQUIRE(expected_strings[i] == actual_strings[i]);
        }
    }
} // namespace test
