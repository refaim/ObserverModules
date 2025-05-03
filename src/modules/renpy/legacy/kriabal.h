#ifndef KRIABAL_KRIABAL_H_
#define KRIABAL_KRIABAL_H_

#include <cstdint>

#include <memory>
#include <string>
#include <vector>

#include "ModuleWin.h"
#include "ModuleDef.h"

#include "_common.h"
#include "_stream.h"

namespace kriabal
{
    class ItemIndexTooLargeError : public RuntimeError {};
    class UserInterrupt : public RuntimeError {};

    struct Item
    {
        int64_t offset = 0;
        int64_t size_in_bytes = 0;
        std::string path;
        std::string header;
    };

    class Tome
    {
    public:
        Tome(std::wstring format, std::string signature) : format_(format), signature_(signature.begin(), signature.end()) {};
        Tome(std::wstring format, std::vector<unsigned char> signature) : format_(format), signature_(signature) {};
        virtual void Open(StorageOpenParams params);
        void FillGeneralInfo(StorageGeneralInfo* info);
        virtual void PrepareItems() = 0;
        const Item& GetItem(size_t index) const;
        void FillItemInfo(const Item& item, StorageItemInfo* output);
        void ExtractItem(const Item& item, const ExtractOperationParams& params);
    protected:
        std::unique_ptr<stream::FileStream> stream_;

        void SkipSignature();
        void ReserveItems(size_t count);
        void PushItem(std::unique_ptr<Item> item);

        static void Assert(bool condition);
    private:
        std::wstring format_;
        std::vector<unsigned char> signature_;
        std::vector<std::unique_ptr<Item>> items_;

        void ReportExtractionProgress(const ExtractProcessCallbacks& callbacks, int64_t num_of_bytes);
    };
}

#endif // KRIABAL_KRIABAL_H_
