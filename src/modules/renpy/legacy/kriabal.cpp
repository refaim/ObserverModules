#include "kriabal.h"

#include <cstdint>

#include <algorithm>
#include <memory>
#include <string>
#include <vector>

namespace kriabal
{
    void Tome::ExtractItem(const Item &item, const ExtractOperationParams &params)
    {
        auto output_stream = std::make_unique<stream::FileStream>(params.DestPath, false, true);

        uint64_t bytes_left = item.size_in_bytes;

        if (!item.header.empty()) {
            output_stream->WriteBytes(item.header, item.header.size());
            bytes_left -= item.header.size();
            ReportExtractionProgress(params.Callbacks, item.header.size());
        }

        auto buffer = std::make_unique<std::string>(32 * 1024, '\0');

        stream_->Seek(item.offset);
        while (bytes_left > 0) {
            const size_t chunk_length = gsl::narrow<size_t>(min(bytes_left, buffer->size()));
            stream_->ReadBytes(*buffer.get(), chunk_length);
            output_stream->WriteBytes(*buffer.get(), chunk_length);

            bytes_left -= chunk_length;
            ReportExtractionProgress(params.Callbacks, chunk_length);
        }
    }

    void Tome::ReportExtractionProgress(const ExtractProcessCallbacks &callbacks, int64_t num_of_bytes)
    {
        if (!callbacks.FileProgress(callbacks.signalContext, num_of_bytes))
            throw UserInterrupt();
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
}
