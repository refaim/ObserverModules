#pragma once

#include <cstddef>

namespace observer::archive_limits
{
    // The Observer ABI exposes 1024 UTF-16 code units. Four UTF-8 bytes per usable code unit is a safe allocation cap.
    inline constexpr std::size_t max_path_bytes = std::size_t{4} * (1024 - 1);

    // The repository corpus currently peaks at 2,205 entries. This leaves ample compatibility headroom while keeping
    // a corrupt count from driving an unbounded allocation.
    inline constexpr std::size_t max_entry_count = 100'000;
} // namespace observer::archive_limits
