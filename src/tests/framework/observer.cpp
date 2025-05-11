#include "observer.h"
#include "../../api.h"

#include <fstream>
#include <iostream>

#include <windows.h>
#include <catch2/catch_test_macros.hpp>

namespace test
{
    class c_module final : public module
    {
    public:
        explicit c_module(const std::string &dll_name)
        {
            dll_ = LoadLibrary(dll_name.c_str());
            if (dll_ == nullptr) {
                throw std::runtime_error("Failed to load DLL");
            }

            const auto load = reinterpret_cast<LoadSubModuleFunc>(GetProcAddress(dll_, "LoadSubModule"));
            unload_module_ = reinterpret_cast<UnloadSubModuleFunc>(GetProcAddress(dll_, "UnloadSubModule"));

            ModuleLoadParameters load_params{};
            load_params.StructSize = sizeof(load_params);
            load_params.Settings = nullptr;
            load(&load_params);
            api_ = load_params.ApiFuncs;
            module_loaded_ = true;
        }

        ~c_module() override
        {
            if (storage_ != nullptr) {
                api_.CloseStorage(storage_);
            }

            if (module_loaded_) {
                unload_module_();
                unload_module_ = nullptr;
            }

            if (dll_ != nullptr) {
                FreeLibrary(dll_);
            }
        }

        bool open(const std::filesystem::path &path)
        {
            std::ifstream input(path, std::ios::binary);
            REQUIRE(input.is_open());
            input.exceptions(std::ofstream::failbit | std::ofstream::badbit);

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

                auto extract = [this, item_index](const std::filesystem::path &path)
                {
                    extract_file(item_index, path);
                };

                files.emplace_back(item.Path, item.Size, item.PackedSize, extract);

                ++item_index;
            }

            return files;
        }

        void extract_file(const int file_index, const std::filesystem::path &path) const
        {
            constexpr ExtractProcessCallbacks callbacks{
                .signalContext = nullptr,
                .FileProgress = [](void *context, int64_t bytes_read)
                {
                    return TRUE;
                },
            };

            const ExtractOperationParams params{
                .ItemIndex = file_index,
                .Flags = 0,
                .DestPath = path.c_str(),
                .Password = nullptr,
                .Callbacks = callbacks,
            };

            REQUIRE(api_.ExtractItem(storage_, params) == SER_SUCCESS);
        }

    private:
        HMODULE dll_;
        bool module_loaded_;
        UnloadSubModuleFunc unload_module_;
        module_cbs api_{};
        HANDLE storage_ = nullptr;
    };

    observer::observer()
    {
        modules_.push_back(std::make_unique<c_module>("renpy.so"));
        modules_.push_back(std::make_unique<c_module>("zanzarah.so"));
    }

    std::vector<file> observer::list_files(const std::filesystem::path &path) const
    {
        c_module *module = nullptr;
        for (const auto &abstract_module: modules_) {
            const auto candidate = dynamic_cast<c_module *>(abstract_module.get());
            REQUIRE(candidate != nullptr);
            if (candidate->open(path)) {
                REQUIRE(module == nullptr);
                module = candidate;
            }
        }
        REQUIRE(module != nullptr);
        // ReSharper disable once CppDFANullDereference
        return module->list_files();
    }
}
