#pragma once
#include "../codecs/crypto_codec.h"
#include <cstddef>
#include <cstdint>
#include <istream>
#include <stdexcept>
#include <vector>
namespace unpackrat::godot
{
    // A hard bound on retained secret material; caller limits can tighten this value.
    inline constexpr std::size_t max_recovered_keys = 128;
    struct key_recovery_limits final
    {
        std::uint64_t max_scanned_bytes = 128ULL * 1024 * 1024;
        std::uint64_t max_instructions = 1024ULL * 1024;
        std::uint64_t max_candidates = 128;
        std::uint64_t max_pattern_instructions = 1024;
    };
    struct key_recovery_budget final
    {
        std::uint64_t scanned_bytes = 0;
        std::uint64_t instructions = 0;
        std::uint64_t candidates = 0;
    };
    class key_recovery_error final : public std::runtime_error
    {
      public:
        using std::runtime_error::runtime_error;
    };
    [[nodiscard]] std::vector<codecs::key256> recover_key_candidates(std::istream &source, key_recovery_budget &budget,
                                                                     const key_recovery_limits &limits = {});
} // namespace unpackrat::godot
