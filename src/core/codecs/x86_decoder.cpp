#include "x86_decoder.h"

#define ZYDIS_STATIC_BUILD
#define ZYCORE_STATIC_BUILD
#include <Zydis/Zydis.h>

namespace unpackrat::codecs
{
    std::optional<x86_instruction> decode_x86(const std::span<const std::byte> input, const std::uint64_t address,
                                              const x86_mode mode)
    {
        ZydisDecoder decoder{};
        const bool wide = mode == x86_mode::x64;
        // Both constant machine/stack pairs are supported by the pinned decoder; initialization
        // cannot fail for these pairs. Only DecodeFull consumes untrusted bytes.
        static_cast<void>(ZydisDecoderInit(&decoder, wide ? ZYDIS_MACHINE_MODE_LONG_64 : ZYDIS_MACHINE_MODE_LEGACY_32,
                                           wide ? ZYDIS_STACK_WIDTH_64 : ZYDIS_STACK_WIDTH_32));
        ZydisDecodedInstruction instruction{};
        std::array<ZydisDecodedOperand, ZYDIS_MAX_OPERAND_COUNT> operands{};
        if (!ZYAN_SUCCESS(ZydisDecoderDecodeFull(&decoder, input.data(), input.size(), &instruction, operands.data())))
            return std::nullopt;
        x86_instruction result;
        static_assert(ZYDIS_MAX_OPERAND_COUNT <= result.addresses.size());
        result.length = instruction.length;
        result.returns = instruction.mnemonic == ZYDIS_MNEMONIC_RET;
        result.bit_xor = instruction.mnemonic == ZYDIS_MNEMONIC_XOR || instruction.mnemonic == ZYDIS_MNEMONIC_XORPS ||
                         instruction.mnemonic == ZYDIS_MNEMONIC_PXOR;
        for (std::uint8_t index = 0; index < instruction.operand_count_visible; ++index) {
            const auto &operand = operands[index];
            if (operand.type == ZYDIS_OPERAND_TYPE_IMMEDIATE) {
                if (operand.imm.is_relative) {
                    if (instruction.mnemonic == ZYDIS_MNEMONIC_CALL) {
                        ZyanU64 target = 0;
                        // A decoded relative CALL has a signed relative immediate; the utility
                        // is infallible under this operand contract (no register context required).
                        static_cast<void>(ZydisCalcAbsoluteAddress(&instruction, &operand, address, &target));
                        result.call_target = target;
                    }
                } else {
                    const auto value = operand.imm.value.u;
                    result.size32 |= value == 32;
                    result.pack_magic |= value == 0x43504447;
                    result.addresses[result.address_count++] = value;
                }
            } else if (operand.type == ZYDIS_OPERAND_TYPE_MEMORY && operand.mem.disp.has_displacement) {
                const bool relative = operand.mem.base == ZYDIS_REGISTER_RIP || operand.mem.base == ZYDIS_REGISTER_EIP;
                const bool absolute =
                    operand.mem.base == ZYDIS_REGISTER_NONE && operand.mem.index == ZYDIS_REGISTER_NONE;
                if (relative || absolute) {
                    ZyanU64 target = 0;
                    // Decoded RIP/EIP-relative or absolute memory has a supported address width.
                    static_cast<void>(ZydisCalcAbsoluteAddress(&instruction, &operand, address, &target));
                    result.addresses[result.address_count++] = target;
                } else if (operand.mem.index != ZYDIS_REGISTER_NONE) {
                    result.indexed_displacement = static_cast<std::uint64_t>(operand.mem.disp.value);
                }
            }
        }
        return result;
    }
} // namespace unpackrat::codecs
