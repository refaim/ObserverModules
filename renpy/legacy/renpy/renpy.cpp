#include "archive.h"

// {9486718F-8F0A-4DE7-9880-01146B336D6B}
static const GUID MODULE_GUID = {0x9486718f, 0x8f0a, 0x4de7, {0x98, 0x80, 0x01, 0x14, 0x6b, 0x33, 0x6d, 0x6b}};

int MODULE_EXPORT LoadSubModule(ModuleLoadParameters *LoadParams) noexcept
{
    return kriabal::submodule::load<renpy::Archive>(2, 0, MODULE_GUID, LoadParams);
}

void MODULE_EXPORT UnloadSubModule() noexcept
{
}
