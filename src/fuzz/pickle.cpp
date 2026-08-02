#include "../modules/renpy/pickle.h"

#include <cstddef>
#include <cstdint>
#include <span>
#include <stdexcept>

extern "C" int LLVMFuzzerTestOneInput(const std::uint8_t *data, const std::size_t size)
{
    const auto bytes = std::span{
        reinterpret_cast<const std::byte *>(data),
        size,
    };

    try {
        static_cast<void>(pickle::loads(bytes));
    } catch (const std::invalid_argument &) {
        // Invalid numeric Pickle input is expected.
        return 0;
    } catch (const std::out_of_range &) {
        // Invalid numeric Pickle input is expected. std::length_error is deliberately not caught.
        return 0;
    } catch (const std::runtime_error &) {
        // Invalid Pickle structure is expected. Allocation failures must still escape.
        return 0;
    }

    return 0;
}
