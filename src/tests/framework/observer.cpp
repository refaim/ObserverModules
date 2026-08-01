#include "observer.h"
#include "../../api.h"
#include "../support/archive_fixtures.h"

#include <array>
#include <cstdint>
#include <format>
#include <fstream>
#include <iterator>
#include <thread>

#include <catch2/catch_test_macros.hpp>
#include <windows.h>

namespace test
{
    enum class module_path_policy : std::uint8_t
    {
        loader_search,
        exact,
    };

    class c_module final : public module
    {
      public:
        explicit c_module(const std::string &dll_name)
            : c_module(std::filesystem::path(dll_name), module_path_policy::loader_search)
        {
        }

        c_module(const std::filesystem::path &dll_path, const module_path_policy path_policy)
        {
            if (path_policy == module_path_policy::exact && !dll_path.is_absolute()) {
                throw std::runtime_error("An exact module path must be absolute");
            }

            const auto load_path =
                path_policy == module_path_policy::exact ? std::filesystem::canonical(dll_path) : dll_path;
            dll_ = path_policy == module_path_policy::exact
                       ? LoadLibraryExW(load_path.c_str(), nullptr,
                                        LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS)
                       : LoadLibraryW(load_path.c_str());
            if (dll_ == nullptr) {
                throw std::runtime_error(
                    std::format("Failed to load {} (Win32 error {})", load_path.string(), GetLastError()));
            }

            try {
                if (path_policy == module_path_policy::exact) {
                    std::wstring actual_path(32'768, L'\0');
                    const auto length =
                        GetModuleFileNameW(dll_, actual_path.data(), static_cast<DWORD>(actual_path.size()));
                    if (length == 0 || length >= actual_path.size()) {
                        throw std::runtime_error("Failed to resolve the loaded package module path");
                    }
                    actual_path.resize(length);
                    if (!std::filesystem::equivalent(load_path, std::filesystem::canonical(actual_path))) {
                        throw std::runtime_error("The loader did not map the requested canonical package module");
                    }
                }

#ifdef __clang__
#pragma clang diagnostic push
#pragma clang diagnostic ignored "-Wcast-function-type-mismatch"
#endif
                load_module_ = reinterpret_cast<LoadSubModuleFunc>(GetProcAddress(dll_, "LoadSubModule"));
                unload_module_ = reinterpret_cast<UnloadSubModuleFunc>(GetProcAddress(dll_, "UnloadSubModule"));
#ifdef __clang__
#pragma clang diagnostic pop
#endif
                if (load_module_ == nullptr || unload_module_ == nullptr) {
                    throw std::runtime_error("The package module does not expose the Observer entry points");
                }

                ModuleLoadParameters load_params{};
                load_params.StructSize = sizeof(load_params);
                load_params.Settings = nullptr;
                if (load_module_(&load_params) == FALSE) {
                    throw std::runtime_error("LoadSubModule rejected its valid package-smoke parameters");
                }
                api_ = load_params.ApiFuncs;
                module_loaded_ = true;
            } catch (...) {
                release();
                throw;
            }
        }

        ~c_module() override
        {
            release();
        }

        bool open(const std::filesystem::path &path)
        {
            REQUIRE(storage_ == nullptr);
            std::ifstream input(path, std::ios::binary);
            REQUIRE(input.is_open());
            input.exceptions(std::ofstream::badbit);

            auto len = 128 * 1024;
            std::string signature;
            signature.resize(len);
            input.read(signature.data(), len);

            StorageOpenParams open_params{
                .StructSize = sizeof(StorageOpenParams),
                .FilePath = path.c_str(),
                .Password = nullptr,
                .Data = signature.c_str(),
                .DataSize = static_cast<size_t>(input.gcount()),
            };

            StorageGeneralInfo info{};
            // TODO mimic observer behavior: call OpenStorage twice with different params
            bool success = api_.OpenStorage(open_params, &storage_, &info) == SOR_SUCCESS;
            if (success) {
                REQUIRE(!std::wstring(info.Format).empty());
                REQUIRE(!std::wstring(info.Compression).empty());
                REQUIRE(!std::wstring(info.Comment).empty());
            }
            return success;
        }

        std::vector<file> list_files()
        {
            REQUIRE(storage_ != nullptr);
            REQUIRE(api_.PrepareFiles(storage_));
            REQUIRE(api_.PrepareFiles(storage_));

            std::vector<file> files{};

            int item_index = 0;
            while (true) {
                StorageItemInfo item{};
                const auto status = api_.GetItem(storage_, item_index, &item);
                if (status == GET_ITEM_NOMOREITEMS) {
                    break;
                }

                REQUIRE(status == GET_ITEM_OK);
                REQUIRE(item.Attributes == FILE_ATTRIBUTE_NORMAL);
                REQUIRE(item.PackedSize > 0);
                REQUIRE(item.Size >= item.PackedSize);
                REQUIRE(item.NumHardlinks == 0);
                REQUIRE(wcslen(item.Path) > 0);

                auto extract = [this, item_index](const std::filesystem::path &path) {
                    extract_file(item_index, path);
                };

                files.emplace_back(item.Path, static_cast<std::int64_t>(item.Size),
                                   static_cast<std::int64_t>(item.PackedSize), extract);

                ++item_index;
            }

            return files;
        }

        [[nodiscard]] const module_cbs &api() const noexcept
        {
            return api_;
        }

        [[nodiscard]] HANDLE storage() const noexcept
        {
            return storage_;
        }

        [[nodiscard]] int load(ModuleLoadParameters *params) const
        {
            return load_module_(params);
        }

        void extract_file(const int file_index, const std::filesystem::path &path) const
        {
            REQUIRE(extract_file_status(file_index, path, [](void *, int64_t) { return TRUE; }) == SER_SUCCESS);
        }

        [[nodiscard]] int extract_file_status(const int file_index, const std::filesystem::path &path,
                                              const ExtractProgressFunc report_progress) const
        {
            const ExtractProcessCallbacks callbacks{
                .signalContext = nullptr,
                .FileProgress = report_progress,
            };

            const ExtractOperationParams params{
                .ItemIndex = file_index,
                .Flags = 0,
                .DestPath = path.c_str(),
                .Password = nullptr,
                .Callbacks = callbacks,
            };

            return api_.ExtractItem(storage_, params);
        }

        void close_storage() noexcept
        {
            if (storage_ != nullptr) {
                api_.CloseStorage(storage_);
                storage_ = nullptr;
            }
        }

      private:
        void release() noexcept
        {
            close_storage();
            if (module_loaded_ && unload_module_ != nullptr) {
                unload_module_();
                module_loaded_ = false;
            }
            unload_module_ = nullptr;
            load_module_ = nullptr;
            if (dll_ != nullptr) {
                static_cast<void>(FreeLibrary(dll_));
                dll_ = nullptr;
            }
        }

        HMODULE dll_ = nullptr;
        bool module_loaded_ = false;
        LoadSubModuleFunc load_module_ = nullptr;
        UnloadSubModuleFunc unload_module_ = nullptr;
        module_cbs api_{};
        HANDLE storage_ = nullptr;
    };

    class temporary_output_file final
    {
      public:
        explicit temporary_output_file(std::filesystem::path path) : path_(std::move(path))
        {
            std::error_code error;
            std::filesystem::remove(path_, error);
        }

        ~temporary_output_file()
        {
            std::error_code error;
            std::filesystem::remove(path_, error);
        }

        temporary_output_file(const temporary_output_file &) = delete;
        temporary_output_file &operator=(const temporary_output_file &) = delete;

        [[nodiscard]] const std::filesystem::path &path() const noexcept
        {
            return path_;
        }

      private:
        std::filesystem::path path_;
    };

    observer::observer()
    {
        modules_.push_back(std::make_unique<c_module>("renpy.so"));
        modules_.push_back(std::make_unique<c_module>("rpgmaker.so"));
        modules_.push_back(std::make_unique<c_module>("zanzarah.so"));
    }

    std::vector<file> observer::list_files(const std::filesystem::path &path) const
    {
        c_module *module = nullptr;
        for (const auto &abstract_module : modules_) {
            const auto candidate = dynamic_cast<c_module *>(abstract_module.get());
            REQUIRE(candidate != nullptr);
            if (candidate->open(path)) {
                REQUIRE(module == nullptr);
                module = candidate;
            }
        }
        REQUIRE(module != nullptr);
        return module->list_files();
    }

    TEST_CASE("module ABI: invalid inputs are rejected", "[contract]")
    {
        constexpr std::array module_names{"renpy.so", "rpgmaker.so", "zanzarah.so"};
        const auto invalid_archive_path =
            std::filesystem::temp_directory_path() /
            std::format(L"observer-invalid-{}.bin", static_cast<unsigned long>(GetCurrentProcessId()));

        for (const auto *module_name : module_names) {
            CAPTURE(module_name);
            c_module loaded(module_name);
            const auto &api = loaded.api();

            std::string invalid_contents;
            if (std::string_view(module_name) == "renpy.so") {
                invalid_contents = "RPA-";
            } else if (std::string_view(module_name) == "rpgmaker.so") {
                invalid_contents = std::string{"RGSSAD\0\3", 8};
            } else {
                invalid_contents.assign(8, '\0');
            }
            {
                std::ofstream invalid_archive(invalid_archive_path, std::ios::binary | std::ios::trunc);
                REQUIRE(invalid_archive.is_open());
                invalid_archive.write(invalid_contents.data(), static_cast<std::streamsize>(invalid_contents.size()));
            }

            StorageGeneralInfo info{};
            HANDLE storage = nullptr;
            const std::array signature{std::byte{0xde}, std::byte{0xad}, std::byte{0xbe}, std::byte{0xef}};
            StorageOpenParams params{
                .StructSize = sizeof(StorageOpenParams),
                .FilePath = invalid_archive_path.c_str(),
                .Password = nullptr,
                .Data = signature.data(),
                .DataSize = signature.size(),
            };

            REQUIRE(api.OpenStorage(params, nullptr, &info) == SOR_INVALID_FILE);
            REQUIRE(api.OpenStorage(params, &storage, nullptr) == SOR_INVALID_FILE);
            params.FilePath = nullptr;
            REQUIRE(api.OpenStorage(params, &storage, &info) == SOR_INVALID_FILE);
            params.FilePath = invalid_archive_path.c_str();
            REQUIRE(loaded.load(nullptr) == FALSE);

            storage = INVALID_HANDLE_VALUE;
            REQUIRE(api.OpenStorage(params, &storage, &info) == SOR_INVALID_FILE);
            REQUIRE(storage == nullptr);

            params.DataSize = 0;
            params.FilePath = L"this-file-does-not-exist.observer-test";
            REQUIRE(api.OpenStorage(params, &storage, &info) == SOR_INVALID_FILE);
            REQUIRE(storage == nullptr);

            REQUIRE(api.PrepareFiles(nullptr) == FALSE);
            REQUIRE(api.GetItem(nullptr, 0, nullptr) == GET_ITEM_ERROR);
            REQUIRE(api.ExtractItem(nullptr, {}) == SER_ERROR_SYSTEM);
            api.CloseStorage(nullptr);

            params.Data = nullptr;
            params.FilePath = invalid_archive_path.c_str();
            REQUIRE(api.OpenStorage(params, &storage, &info) == SOR_SUCCESS);
            REQUIRE(storage != nullptr);

            REQUIRE(api.PrepareFiles(storage) == FALSE);
            REQUIRE(api.GetItem(storage, -1, nullptr) == GET_ITEM_ERROR);
            REQUIRE(api.GetItem(storage, 0, nullptr) == GET_ITEM_ERROR);

            StorageItemInfo item{};
            REQUIRE(api.GetItem(storage, 0, &item) == GET_ITEM_NOMOREITEMS);

            ExtractOperationParams extract_params{
                .ItemIndex = -1,
                .Flags = 0,
                .DestPath = invalid_archive_path.c_str(),
                .Password = nullptr,
                .Callbacks = {},
            };
            REQUIRE(api.ExtractItem(storage, extract_params) == SER_ERROR_SYSTEM);
            extract_params.ItemIndex = 0;
            extract_params.DestPath = nullptr;
            REQUIRE(api.ExtractItem(storage, extract_params) == SER_ERROR_SYSTEM);
            extract_params.DestPath = invalid_archive_path.c_str();
            extract_params.Callbacks.FileProgress = [](void *, int64_t) { return TRUE; };
            REQUIRE(api.ExtractItem(storage, extract_params) == SER_ERROR_SYSTEM);
            extract_params.Callbacks.FileProgress = nullptr;
            REQUIRE(api.ExtractItem(storage, extract_params) == SER_ERROR_SYSTEM);

            api.CloseStorage(storage);

            const auto expect_current_file_prepare_failure = [&] {
                StorageOpenParams corrupt_params{
                    .StructSize = sizeof(StorageOpenParams),
                    .FilePath = invalid_archive_path.c_str(),
                    .Password = nullptr,
                    .Data = nullptr,
                    .DataSize = 0,
                };
                HANDLE corrupt_storage = nullptr;
                REQUIRE(api.OpenStorage(corrupt_params, &corrupt_storage, &info) == SOR_SUCCESS);
                REQUIRE(corrupt_storage != nullptr);
                REQUIRE(api.PrepareFiles(corrupt_storage) == FALSE);
                api.CloseStorage(corrupt_storage);
            };

            const auto expect_prepare_failure = [&](const std::string_view contents) {
                {
                    std::ofstream invalid_archive(invalid_archive_path, std::ios::binary | std::ios::trunc);
                    REQUIRE(invalid_archive.is_open());
                    invalid_archive.write(contents.data(), static_cast<std::streamsize>(contents.size()));
                }
                expect_current_file_prepare_failure();
            };

            if (std::string_view(module_name) == "renpy.so") {
                expect_prepare_failure("RPA-2.0 0000000000000000\n");
                expect_prepare_failure("RPA-2.0 -000000000000001\n");
                expect_prepare_failure("RPA-2.0 ffffffffffffffff\n");
                expect_prepare_failure("RPA-2.0 0000000000000100\n");
                expect_prepare_failure("RPA-3.0 0000000000000020 0000000000000001\n");

                const support::temporary_sparse_renpy_archive sparse_archive;
                REQUIRE((GetFileAttributesW(sparse_archive.path().c_str()) & FILE_ATTRIBUTE_SPARSE_FILE) != 0);
                REQUIRE(std::filesystem::file_size(sparse_archive.path()) > 64ULL * 1024 * 1024);
                StorageOpenParams sparse_params{
                    .StructSize = sizeof(StorageOpenParams),
                    .FilePath = sparse_archive.path().c_str(),
                    .Password = nullptr,
                    .Data = nullptr,
                    .DataSize = 0,
                };
                HANDLE sparse_storage = nullptr;
                REQUIRE(api.OpenStorage(sparse_params, &sparse_storage, &info) == SOR_SUCCESS);
                REQUIRE(sparse_storage != nullptr);
                REQUIRE(api.PrepareFiles(sparse_storage) == FALSE);
                api.CloseStorage(sparse_storage);
            } else if (std::string_view(module_name) == "zanzarah.so") {
                expect_prepare_failure(std::string(4, '\0'));
                expect_prepare_failure(std::string{"\0\0\0\0\xff\xff\xff\xff", 8});
            }
        }

        std::error_code error;
        std::filesystem::remove(invalid_archive_path, error);
    }

    TEST_CASE("module ABI: malformed RenPy index shapes are rejected", "[contract]")
    {
        const auto expect_rejected = [](const std::string_view label, const support::byte_buffer &pickle_index) {
            const support::temporary_archive archive(label, support::make_renpy_archive_with_index(pickle_index));
            c_module loaded("renpy.so");
            REQUIRE(loaded.open(archive.path()));
            REQUIRE(loaded.api().PrepareFiles(loaded.storage()) == FALSE);
        };

        expect_rejected("renpy-empty-properties", {'}', 'U', 1, 'x', ']', 's', '.'});
        expect_rejected("renpy-short-tuple", {'}', 'U', 1, 'x', ']', ')', 'a', 's', '.'});
    }

    TEST_CASE("module ABI: RPG Maker rejects declared paths outside its resource bounds", "[contract]")
    {
        const auto expect_rejected = [](const std::string_view label, const support::byte_buffer &contents) {
            const support::temporary_archive archive(label, contents);
            c_module loaded("rpgmaker.so");
            REQUIRE(loaded.open(archive.path()));
            REQUIRE(loaded.api().PrepareFiles(loaded.storage()) == FALSE);
        };

        expect_rejected("rpgmaker-path-budget", {'R', 'G', 'S', 'S', 'A', 'D', 0,  3, 1, 0, 0,    0,    13,   0,
                                                 0,   0,   13,  0,   0,   0,   13, 0, 0, 0, 0xf3, 0xff, 0xff, 0xff});
        expect_rejected("rpgmaker-path-range", {'R', 'G', 'S', 'S', 'A', 'D', 0,  3, 1, 0, 0,  0, 13, 0,
                                                0,   0,   13,  0,   0,   0,   13, 0, 0, 0, 14, 0, 0,  0});
    }

    TEST_CASE("module ABI: Zanzarah rejects metadata outside its resource and body bounds", "[contract]")
    {
        const auto expect_rejected = [](const std::string_view label, const support::byte_buffer &contents) {
            const support::temporary_archive archive(label, contents);
            c_module loaded("zanzarah.so");
            REQUIRE(loaded.open(archive.path()));
            REQUIRE(loaded.api().PrepareFiles(loaded.storage()) == FALSE);
        };

        expect_rejected("zanzarah-entry-count", {0, 0, 0, 0, 0xff, 0xff, 0xff, 0x7f});
        expect_rejected("zanzarah-entry-range", {0, 0, 0, 0, 1, 0, 0, 0});
        expect_rejected("zanzarah-path-size",
                        {0, 0, 0, 0, 1, 0, 0, 0, 0xff, 0xff, 0xff, 0x7f, 0, 0, 0, 0, 0, 0, 0, 0, 0});
        expect_rejected("zanzarah-path-range",
                        {0, 0, 0, 0, 1, 0, 0, 0, 10, 0, 0, 0, 'a', 'b', 'c', 'd', 'e', 'f', 'g', 'h', 'i'});
        expect_rejected("zanzarah-small-block", {0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 'a', 0, 0, 0, 0, 1, 0, 0, 0});
        expect_rejected("zanzarah-body-range",
                        {0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 'a', 64, 0, 0, 0, 5, 0, 0, 0, 0, 0, 0, 0, 'x'});
        expect_rejected("zanzarah-body-size",
                        {0, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 'a', 0, 0, 0, 0, 5, 0, 0, 0, 0, 0, 0, 0});
    }

    TEST_CASE("module ABI: invalid RenPy body ranges are read errors", "[contract]")
    {
        const auto expect_read_error = [](const std::string_view label, const support::byte_buffer &pickle_index) {
            const support::temporary_archive archive(label, support::make_renpy_archive_with_index(pickle_index));
            c_module loaded("renpy.so");
            REQUIRE(loaded.open(archive.path()));
            REQUIRE(loaded.api().PrepareFiles(loaded.storage()) == TRUE);

            const auto destination = archive.path().wstring() + L".out";
            REQUIRE(loaded.extract_file_status(0, destination, [](void *, int64_t) { return TRUE; }) == SER_ERROR_READ);
            std::error_code error;
            std::filesystem::remove(destination, error);
        };

        expect_read_error("renpy-negative-offset",
                          {'}', 'U', 1, 'x', ']', 'I', '-', '1', '\n', 'K', 1, 0x86, 'a', 's', '.'});
        expect_read_error("renpy-truncated-body", {'}', 'U', 1, 'x', ']', 'K', 0, 'K', 100, 0x86, 'a', 's', '.'});
    }

    TEST_CASE("module ABI: progress cancellation aborts extraction", "[contract]")
    {
        const std::string payload(std::size_t{256} * 1024, 'x');
        const support::temporary_archive archive("abort", support::make_rpgmaker_archive("abort.txt", payload));
        c_module loaded("rpgmaker.so");
        REQUIRE(loaded.open(archive.path()));
        REQUIRE(loaded.list_files().size() == 1);

        const auto destination = archive.path().wstring() + L".out";
        REQUIRE(loaded.extract_file_status(0, destination, [](void *, int64_t) { return FALSE; }) == SER_USERABORT);
        REQUIRE(loaded.extract_file_status(0, destination, [](void *, int64_t) -> int {
            throw std::runtime_error("callback");
        }) == SER_ERROR_SYSTEM);
        REQUIRE(loaded.extract_file_status(0, std::filesystem::temp_directory_path(),
                                           [](void *, int64_t) { return TRUE; }) == SER_ERROR_WRITE);

        const auto pipe_name = std::format(L"\\\\.\\pipe\\observer-write-failure-{}", GetCurrentProcessId());
        const auto pipe = CreateNamedPipeW(pipe_name.c_str(), PIPE_ACCESS_INBOUND | FILE_FLAG_FIRST_PIPE_INSTANCE,
                                           PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT, 1, 0, 1, 0, nullptr);
        REQUIRE(pipe != INVALID_HANDLE_VALUE);
        std::jthread pipe_server([pipe] {
            static_cast<void>(ConnectNamedPipe(pipe, nullptr));
            static_cast<void>(CloseHandle(pipe));
        });
        REQUIRE(loaded.extract_file_status(0, pipe_name, [](void *, int64_t) { return TRUE; }) == SER_ERROR_WRITE);

        std::error_code error;
        std::filesystem::remove(destination, error);
    }

    TEST_CASE("module ABI: an item path must fit the ABI buffer", "[contract]")
    {
        StorageItemInfo item{};
        const std::string oversized_path(std::size(item.Path), 'a');
        const support::temporary_archive archive("oversized-path",
                                                 support::make_rpgmaker_archive(oversized_path, "payload"));
        c_module loaded("rpgmaker.so");
        REQUIRE(loaded.open(archive.path()));
        REQUIRE(loaded.api().PrepareFiles(loaded.storage()) == TRUE);

        REQUIRE(loaded.api().GetItem(loaded.storage(), 0, &item) == GET_ITEM_ERROR);
    }

    TEST_CASE("module ABI: a large valid Zanzarah metadata table is enumerated", "[contract][metadata]")
    {
        constexpr std::size_t entry_count = 4'096;
        const support::temporary_archive archive("zanzarah-large-metadata",
                                                 support::make_zanzarah_archive_with_entries(entry_count));
        c_module loaded("zanzarah.so");
        REQUIRE(loaded.open(archive.path()));
        REQUIRE(loaded.api().PrepareFiles(loaded.storage()) == TRUE);

        for (std::size_t index = 0; index < entry_count; ++index) {
            StorageItemInfo item{};
            REQUIRE(loaded.api().GetItem(loaded.storage(), static_cast<int>(index), &item) == GET_ITEM_OK);
            REQUIRE(item.Size == 1);
            REQUIRE(item.PackedSize == 1);
        }

        StorageItemInfo item{};
        REQUIRE(loaded.api().GetItem(loaded.storage(), static_cast<int>(entry_count), &item) == GET_ITEM_NOMOREITEMS);
    }

    TEST_CASE("module ABI: RenPy rejects an index that expands beyond its metadata budget", "[contract][metadata]")
    {
        constexpr std::size_t expanded_size = 64ULL * 1024 * 1024 + 1;
        const support::temporary_archive archive("renpy-expanded-metadata",
                                                 support::make_renpy_archive_with_expanded_index(expanded_size));
        c_module loaded("renpy.so");
        REQUIRE(loaded.open(archive.path()));
        REQUIRE(loaded.api().PrepareFiles(loaded.storage()) == FALSE);
    }

    TEST_CASE("package runtime smoke loads an exact unpacked module", "[package-smoke][.]")
    {
        const auto require_environment = [](const wchar_t *name) {
            const auto required_size = GetEnvironmentVariableW(name, nullptr, 0);
            if (required_size == 0) {
                throw std::runtime_error(std::format("Required package-smoke environment variable is absent: {}",
                                                     std::filesystem::path(name).string()));
            }
            std::wstring value(required_size, L'\0');
            const auto written = GetEnvironmentVariableW(name, value.data(), required_size);
            if (written == 0 || written >= required_size) {
                throw std::runtime_error("Failed to read a package-smoke environment variable");
            }
            value.resize(written);
            return value;
        };

        const std::filesystem::path module_path(require_environment(L"OBSERVER_PACKAGE_MODULE"));
        const auto format = require_environment(L"OBSERVER_PACKAGE_FORMAT");
        REQUIRE(module_path.is_absolute());
        REQUIRE(std::filesystem::is_regular_file(module_path));

        support::byte_buffer archive_contents;
        std::wstring expected_path;
        std::string expected_payload;
        if (format == L"renpy") {
            expected_path = L"package\\hello.txt";
            expected_payload = "renpy package smoke";
            archive_contents = support::make_renpy_archive("package/hello.txt", expected_payload);
        } else if (format == L"rpgmaker") {
            expected_path = L"Data\\package.txt";
            expected_payload = "rpgmaker package smoke";
            archive_contents = support::make_rpgmaker_archive("Data\\package.txt", expected_payload);
        } else if (format == L"zanzarah") {
            expected_path = L"data\\package.txt";
            expected_payload = "zanzarah package smoke";
            archive_contents = support::make_zanzarah_archive("..\\data\\package.txt", expected_payload);
        } else {
            throw std::runtime_error("OBSERVER_PACKAGE_FORMAT expects renpy, rpgmaker, or zanzarah");
        }

        const support::temporary_archive archive("package-smoke", archive_contents);
        c_module loaded(module_path, module_path_policy::exact);
        REQUIRE(loaded.open(archive.path()));
        const auto files = loaded.list_files();
        REQUIRE(files.size() == 1);
        REQUIRE(files.front().path == expected_path);
        REQUIRE(files.front().uncompressed_size == static_cast<int64_t>(expected_payload.size()));
        REQUIRE(files.front().compressed_size == static_cast<int64_t>(expected_payload.size()));

        const temporary_output_file destination(archive.path().wstring() + L".out");
        REQUIRE(loaded.extract_file_status(0, destination.path(), [](void *, int64_t) { return TRUE; }) == SER_SUCCESS);
        loaded.close_storage();
        REQUIRE(loaded.storage() == nullptr);
        std::ifstream extracted(destination.path(), std::ios::binary);
        REQUIRE(extracted.is_open());
        const std::string actual_payload{std::istreambuf_iterator<char>(extracted), std::istreambuf_iterator<char>()};
        REQUIRE(actual_payload == expected_payload);
    }
} // namespace test
