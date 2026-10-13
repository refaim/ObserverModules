#include "api.h"
#include "archive.h"
#include "modules/extractor.h"

#include <cstddef>
#include <cstring>
#include <filesystem>
#include <memory>
#include <new>
#include <span>
#include <string>

#ifdef _DEBUG
#include <crtdbg.h>
#include <cstdlib>
#endif

namespace
{
#ifdef _DEBUG
    void configure_debug_crt() noexcept
    {
        _set_abort_behavior(0, _WRITE_ABORT_MSG | _CALL_REPORTFAULT);
        _CrtSetReportMode(_CRT_WARN, _CRTDBG_MODE_FILE);
        _CrtSetReportFile(_CRT_WARN, _CRTDBG_FILE_STDERR);
        _CrtSetReportMode(_CRT_ERROR, _CRTDBG_MODE_FILE);
        _CrtSetReportFile(_CRT_ERROR, _CRTDBG_FILE_STDERR);
        _CrtSetReportMode(_CRT_ASSERT, _CRTDBG_MODE_FILE);
        _CrtSetReportFile(_CRT_ASSERT, _CRTDBG_FILE_STDERR);
    }
#endif
} // namespace

void copy_string(const std::wstring &source, wchar_t *destination, const std::size_t max_len) noexcept
{
    static_cast<void>(wcsncpy_s(destination, max_len, source.c_str(), _TRUNCATE));
}

extern "C" int MODULE_EXPORT OpenStorage(StorageOpenParams params, HANDLE *storage, StorageGeneralInfo *info)
{
    if (storage != nullptr) {
        *storage = nullptr;
    }
    if (info != nullptr) {
        std::memset(info, 0, sizeof(StorageGeneralInfo));
    }
    if (storage == nullptr || info == nullptr || params.StructSize != sizeof(StorageOpenParams) ||
        params.FilePath == nullptr) {
        return SOR_INVALID_FILE;
    }

    // Copy the packed pointer into an aligned local before binding a C++ reference.
    const auto file_path = params.FilePath;
    const std::filesystem::path path(file_path);

    std::span<const std::byte> data;
    if (params.Data != nullptr && params.DataSize > 0) {
        data = std::span(static_cast<const std::byte *>(params.Data), params.DataSize);
    }

    auto archive = std::make_unique<archive::archive>(std::make_unique<extractor::extractor>());
    try {
        auto [format, compression, comment] = archive->open(
            path, data,
            params.Password == nullptr ? std::string_view{}
                                       : std::string_view(params.Password, strnlen_s(params.Password, 65)));

        alignas(std::max_align_t) StorageGeneralInfo local_info;
        std::memset(&local_info, 0, sizeof(StorageGeneralInfo));
        copy_string(format, local_info.Format, STORAGE_FORMAT_NAME_MAX_LEN);
        copy_string(compression.empty() ? std::wstring(L"-") : compression, local_info.Compression,
                    STORAGE_PARAM_MAX_LEN);
        copy_string(comment.empty() ? std::wstring(L"-") : comment, local_info.Comment, STORAGE_PARAM_MAX_LEN);

        std::memcpy(info, &local_info, sizeof(StorageGeneralInfo));

        *storage = static_cast<HANDLE>(archive.release());
    } catch (const extractor::password_required &) {
        return SOR_PASSWORD_REQUIRED;
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

    std::unique_ptr<archive::archive> archive(static_cast<archive::archive *>(storage));
    archive.reset();
}

extern "C" int MODULE_EXPORT PrepareFiles(HANDLE storage)
{
    if (storage == nullptr) {
        return FALSE;
    }

    auto *archive = static_cast<archive::archive *>(storage);
    try {
        archive->prepare_files();
    } catch (std::runtime_error &) {
        return FALSE;
    } catch (std::logic_error &) {
        return FALSE;
    }

    return TRUE;
}

extern "C" int MODULE_EXPORT GetItem(HANDLE storage, int item_index, StorageItemInfo *item_info)
{
    if (storage == nullptr || item_index < 0 || item_info == nullptr) {
        return GET_ITEM_ERROR;
    }

    const auto *archive = static_cast<archive::archive *>(storage);
    try {
        const auto &file = archive->get_file(item_index);
        const auto header_size = std::ssize(file.header);

        alignas(std::max_align_t) StorageItemInfo local_info;
        std::memset(&local_info, 0, sizeof(StorageItemInfo));
        local_info.Attributes = FILE_ATTRIBUTE_NORMAL;
        local_info.Size = header_size + file.uncompressed_body_size_in_bytes;
        local_info.PackedSize = header_size + file.compressed_body_size_in_bytes;
        local_info.NumHardlinks = 0;
        const auto chars_written = MultiByteToWideChar(CP_UTF8, 0, file.path.c_str(), -1, local_info.Path,
                                                       static_cast<int>(std::size(local_info.Path)));
        std::memcpy(item_info, &local_info, sizeof(StorageItemInfo));
        if (chars_written == 0) {
            return GET_ITEM_ERROR;
        }
    } catch (std::out_of_range &) {
        return GET_ITEM_NOMOREITEMS;
    }

    return GET_ITEM_OK;
}

extern "C" int MODULE_EXPORT ExtractItem(HANDLE storage, ExtractOperationParams params)
{
    if (storage == nullptr || params.ItemIndex < 0 || params.DestPath == nullptr ||
        params.Callbacks.FileProgress == nullptr) {
        return SER_ERROR_SYSTEM;
    }

    auto report_progress = [callbacks = params.Callbacks](const int64_t bytes_read) {
        if (!callbacks.FileProgress(callbacks.signalContext, bytes_read)) {
            throw archive::user_interrupt();
        }
    };

    const auto *archive = static_cast<archive::archive *>(storage);
    try {
        // Copy the packed pointer into an aligned local before binding a C++ reference.
        const auto destination = params.DestPath;
        archive->extract_file(params.ItemIndex, destination, report_progress);
    } catch (archive::user_interrupt &) {
        return SER_USERABORT;
    } catch (archive::read_error &) {
        return SER_ERROR_READ;
    } catch (archive::write_error &) {
        return SER_ERROR_WRITE;
    } catch (const std::bad_alloc &) {
        // Allocation failure is terminal. Preserve stack unwinding and partial-output cleanup,
        // then translate it once at the C ABI boundary without retrying or reporting success.
        return SER_ERROR_SYSTEM;
    } catch (std::runtime_error &) {
        return SER_ERROR_SYSTEM;
    } catch (std::logic_error &) {
        return SER_ERROR_SYSTEM;
    }

    return SER_SUCCESS;
}

extern "C" int MODULE_EXPORT LoadSubModule(ModuleLoadParameters *LoadParams) noexcept
{
#ifdef _DEBUG
    configure_debug_crt();
#endif
    if (LoadParams == nullptr || LoadParams->StructSize != sizeof(ModuleLoadParameters)) {
        return FALSE;
    }

    extractor::configure(LoadParams->Settings);
    const auto [id, major_version, minor_version] = extractor::get_version_info();
    const auto [id1, id2, id3, id4] = id;
    LoadParams->ModuleId = {id1, id2, id3, {id4[0], id4[1], id4[2], id4[3], id4[4], id4[5], id4[6], id4[7]}};
    LoadParams->ModuleVersion = MAKEMODULEVERSION(major_version, minor_version);
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
    extractor::reset_settings();
}
