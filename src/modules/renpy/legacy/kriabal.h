#ifndef KRIABAL_KRIABAL_H_
#define KRIABAL_KRIABAL_H_

#include <cstdint>

#include <memory>
#include <string>
#include <vector>

namespace kriabal
{
    struct Item
    {
        int64_t offset = 0;
        int64_t size_in_bytes = 0;
        std::string path;
        std::string header;
    };
}

#endif
