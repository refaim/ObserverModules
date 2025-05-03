// This is an open source non-commercial project. Dear PVS-Studio, please check it.
// PVS-Studio Static Code Analyzer for C, C++, C#, and Java: https://pvs-studio.com

#include "stdafx.h"

#include "kriabal.h"

#include <cstdint>
#include <cstring>

#include <algorithm>
#include <memory>
#include <string>
#include <vector>

#include <gsl/gsl>

#include "ModuleWin.h"
#include "ModuleDef.h"

#include "_common.h"
#include "_stream.h"

namespace kriabal
{
    void CopyString(std::wstring source, gsl::span<wchar_t> destination, int32_t max_len)
    {
        if (wcscpy_s(destination.data(), max_len, source.c_str()) != 0)
            throw RuntimeError();
    }

    bool DataMatchesSignature(gsl::span<const char> buffer, const std::vector<unsigned char>& signature) noexcept
    {
        if (signature.size() > buffer.size()) return false;

        for (size_t i = 0; i < signature.size(); ++i)
            if (gsl::at(signature, i) != gsl::at(buffer, i))
                return false;

        return true;
    }

    void Tome::Open(StorageOpenParams params)
    {
        if (params.Data != nullptr && !DataMatchesSignature(gsl::make_span(static_cast<const char*>(params.Data), params.DataSize), signature_))
            throw RuntimeError();

        stream_ = std::make_unique<stream::FileStream>(params.FilePath, true, false);
    }

    void Tome::FillGeneralInfo(StorageGeneralInfo* info)
    {
        std::memset(info, 0, sizeof(StorageGeneralInfo));
        CopyString(format_, info->Format, STORAGE_FORMAT_NAME_MAX_LEN);
        CopyString(L"-", info->Compression, STORAGE_PARAM_MAX_LEN);
        CopyString(L"-", info->Comment, STORAGE_PARAM_MAX_LEN);
    }

    const Item& Tome::GetItem(size_t index) const
    {
        if (index < 0) throw RuntimeError();
        if (gsl::narrow<size_t>(index) >= items_.size()) throw ItemIndexTooLargeError();
        return *gsl::at(items_, index).get();
    }

    void Tome::FillItemInfo(const Item& item, StorageItemInfo* output)
    {
        std::memset(output, 0, sizeof(StorageItemInfo));
        output->Attributes = FILE_ATTRIBUTE_NORMAL;
        output->Size = item.size_in_bytes;
        output->PackedSize = output->Size;
        Assert(MultiByteToWideChar(CP_UTF8, 0, item.path.c_str(), -1, output->Path, STRBUF_SIZE(output->Path)));
    }

    void Tome::ExtractItem(const Item& item, const ExtractOperationParams& params)
    {
        auto output_stream = std::make_unique<stream::FileStream>(params.DestPath, false, true);

        uint64_t bytes_left = item.size_in_bytes;

        if (!item.header.empty())
        {
            output_stream->WriteBytes(item.header, item.header.size());
            bytes_left -= item.header.size();
            ReportExtractionProgress(params.Callbacks, item.header.size());
        }

        auto buffer = std::make_unique<std::string>(32 * 1024, '\0');

        stream_->Seek(item.offset);
        while (bytes_left > 0)
        {
            const size_t chunk_length = gsl::narrow<size_t>(min(bytes_left, buffer->size()));
            stream_->ReadBytes(*buffer.get(), chunk_length);
            output_stream->WriteBytes(*buffer.get(), chunk_length);

            bytes_left -= chunk_length;
            ReportExtractionProgress(params.Callbacks, chunk_length);
        }
    }

    void Tome::ReportExtractionProgress(const ExtractProcessCallbacks& callbacks, int64_t num_of_bytes)
    {
        if (!callbacks.FileProgress(callbacks.signalContext, num_of_bytes))
            throw UserInterrupt();
    }

    void Tome::SkipSignature()
    {
        stream_->Seek(signature_.size());
    }

    void Tome::ReserveItems(size_t count)
    {
        items_.reserve(count);
    }

    void Tome::PushItem(std::unique_ptr<Item> item)
    {
        std::replace(item->path.begin(), item->path.end(), '/', '\\');
        items_.push_back(std::move(item));
    }

    void Tome::Assert(bool condition)
    {
        if (!condition)
            throw RuntimeError();
    }
}
