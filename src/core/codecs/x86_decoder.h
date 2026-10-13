#pragma once
#include <array>
#include <cstddef>
#include <cstdint>
#include <optional>
#include <span>
namespace unpackrat::codecs
{
    enum class x86_mode : std::uint8_t
    {
        x86,
        x64
    };
    struct x86_instruction final
    {
        std::array<std::uint64_t, 10> addresses{};
        std::optional<std::uint64_t> call_target;
        // A syntactic indexed-memory displacement, never a runtime-resolved address.
        std::optional<std::uint64_t> indexed_displacement;
        std::uint8_t length = 0;
        std::uint8_t address_count = 0;
        bool size32 = false;
        bool pack_magic = false;
        bool bit_xor = false;
        bool returns = false;
    };
    [[nodiscard]] std::optional<x86_instruction> decode_x86(std::span<const std::byte> input, std::uint64_t address,
                                                            x86_mode mode);
} // namespace unpackrat::codecs
