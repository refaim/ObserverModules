#include <filesystem>
#include <memory>
#include <string>

#include "api.h"

#include "zanzarah/zanzarah.h"

void copy_string(const std::wstring &source, wchar_t *destination, const std::size_t max_len)
{
    if (wcscpy_s(destination, max_len, source.c_str()) != 0) {
        throw std::runtime_error("CopyString failed");
    }
}

extern "C" int MODULE_EXPORT OpenStorage(StorageOpenParams params, HANDLE *storage, StorageGeneralInfo *info)
{
    if (storage == nullptr) return SOR_INVALID_FILE;

    const std::filesystem::path path(params.FilePath);

    std::span<const std::byte> data;
    if (params.Data != nullptr && params.DataSize > 0) {
        data = std::span(static_cast<const std::byte *>(params.Data), params.DataSize);
    }

    auto archive = std::make_unique<shared::archive>();
    try {
        auto [format, compression, comment] = archive->open(path, data);

        std::memset(info, 0, sizeof(StorageGeneralInfo));
        copy_string(format, info->Format, STORAGE_FORMAT_NAME_MAX_LEN);
        copy_string(compression.empty() ? std::wstring(L"-") : compression, info->Compression, STORAGE_PARAM_MAX_LEN);
        copy_string(comment.empty() ? std::wstring(L"-") : comment, info->Comment, STORAGE_PARAM_MAX_LEN);

        *storage = static_cast<HANDLE>(archive.release());
    } catch (std::runtime_error &) {
        return SOR_INVALID_FILE;
    }

    return SOR_SUCCESS;
}

extern "C" void MODULE_EXPORT CloseStorage(HANDLE storage)
{
    if (storage == nullptr) {
        return;
    }

    std::unique_ptr<shared::archive> archive(static_cast<shared::archive *>(storage));
    archive.reset();
}

extern "C" int MODULE_EXPORT PrepareFiles(HANDLE storage)
{
    if (storage == nullptr) {
        return FALSE;
    }

    const auto archive = static_cast<shared::archive *>(storage);
    try {
        archive->prepare_files();
    } catch (std::runtime_error &) {
        return FALSE;
    }

    return TRUE;
}

extern "C" int MODULE_EXPORT GetItem(HANDLE storage, int item_index, StorageItemInfo *item_info)
{
    if (storage == nullptr || item_index < 0) {
        return GET_ITEM_ERROR;
    }

    const auto archive = static_cast<shared::archive *>(storage);
    try {
        const auto file = archive->get_file(item_index);

        std::memset(item_info, 0, sizeof(StorageItemInfo));
        item_info->Attributes = FILE_ATTRIBUTE_NORMAL;
        item_info->Size = file.uncompressed_size_in_bytes;
        item_info->PackedSize = file.compressed_size_in_bytes;
        const auto chars_written = MultiByteToWideChar(CP_UTF8, 0, file.path.c_str(), -1, item_info->Path,
                                                       static_cast<int>(std::size(item_info->Path)));
        if (chars_written == 0) {
            return GET_ITEM_ERROR;
        }
    } catch (std::out_of_range &) {
        return GET_ITEM_NOMOREITEMS;
    } catch (std::runtime_error &) {
        return GET_ITEM_ERROR;
    }

    return GET_ITEM_OK;
}

// ReSharper disable once CppParameterMayBeConst
extern "C" int MODULE_EXPORT ExtractItem(HANDLE storage, ExtractOperationParams params)
{
    if (storage == nullptr || params.ItemIndex < 0 || params.DestPath == nullptr) {
        return SER_ERROR_SYSTEM;
    }

    auto report_progress = [callbacks = params.Callbacks](const int64_t bytes_read)
    {
        if (!callbacks.FileProgress(callbacks.signalContext, bytes_read)) {
            throw shared::user_interrupt();
        }
    };

    const auto archive = static_cast<shared::archive *>(storage);
    try {
        archive->extract_file(params.ItemIndex, params.DestPath, report_progress);
    } catch (shared::user_interrupt &) {
        return SER_USERABORT;
    } catch (shared::read_error &) {
        return SER_ERROR_READ;
    } catch (shared::write_error &) {
        return SER_ERROR_WRITE;
    } catch (std::runtime_error &) {
        return SER_ERROR_SYSTEM;
    }

    return SER_SUCCESS;
}

extern "C" int MODULE_EXPORT LoadSubModule(ModuleLoadParameters *LoadParams) noexcept
{
    LoadParams->ModuleId = {0x86e7e4c3, 0xbc44, 0x4e8e, {0x90, 0xaf, 0xbd, 0xbd, 0x1c, 0xb6, 0x1a, 0x83}};
    LoadParams->ModuleVersion = MAKEMODULEVERSION(2, 0);
    LoadParams->ApiVersion = ACTUAL_API_VERSION;
    LoadParams->ApiFuncs.OpenStorage = OpenStorage;
    LoadParams->ApiFuncs.CloseStorage = CloseStorage;
    LoadParams->ApiFuncs.GetItem = GetItem;
    LoadParams->ApiFuncs.ExtractItem = ExtractItem;
    LoadParams->ApiFuncs.PrepareFiles = PrepareFiles;
    return TRUE;
}

extern "C" void MODULE_EXPORT UnloadSubModule() noexcept
{
}
