#include "godot_key_fixtures.h"
#include "godot_fixtures.h"
#include <algorithm>
#include <string_view>
namespace test::support
{
    key_fixture make_key_fixture(const key_shape shape, const bool x64)
    {
        godot_bytes bytes(0x1600, 0);
        const std::uint64_t base = x64 ? 0x140000000ULL : 0x400000;
        const std::size_t optional = x64 ? 0xf0 : 0xe0;
        bytes[0] = 'M';
        bytes[1] = 'Z';
        patch_u32(bytes, 0x3c, 0x80);
        patch_u32(bytes, 0x80, 0x4550);
        patch_u16(bytes, 0x84, x64 ? 0x8664 : 0x14c);
        patch_u16(bytes, 0x86, 2);
        patch_u16(bytes, 0x94, static_cast<std::uint16_t>(optional));
        patch_u16(bytes, 0x98, x64 ? 0x20b : 0x10b);
        if (x64)
            patch_u64(bytes, 0xb0, base);
        else
            patch_u32(bytes, 0xb4, static_cast<std::uint32_t>(base));
        const auto section = [&](std::size_t at, std::string_view name, std::uint32_t rva, std::uint32_t raw,
                                 std::uint32_t raw_size, std::uint32_t virtual_size, std::uint32_t flags) {
            std::copy(name.begin(), name.end(), bytes.begin() + static_cast<std::ptrdiff_t>(at));
            patch_u32(bytes, at + 8, virtual_size);
            patch_u32(bytes, at + 12, rva);
            patch_u32(bytes, at + 16, raw_size);
            patch_u32(bytes, at + 20, raw);
            patch_u32(bytes, at + 36, flags);
        };
        section(0x98 + optional, ".text", 0x1000, 0x200, 0x1000, 0x1000, 0x60000020);
        section(0xc0 + optional, ".rdata", 0x2000, 0x1200, 0x400, 0x800, 0x40000040);
        std::fill(bytes.begin() + 0x200, bytes.begin() + 0x1200, 0xcc);
        std::size_t cursor = 0x200;
        const auto emit = [&](std::initializer_list<std::uint8_t> code) {
            for (auto value : code)
                bytes[cursor++] = value;
        };
        const auto u32 = [&](std::uint32_t value) {
            patch_u32(bytes, cursor, value);
            cursor += 4;
        };
        const auto reference = [&](std::uint64_t target) {
            if (x64) {
                emit({0x48, 0x8d, 0x0d});
                const auto next = base + 0x1000 + cursor - 0x200 + 4;
                u32(static_cast<std::uint32_t>(target - next));
            } else {
                emit({0xb9});
                u32(static_cast<std::uint32_t>(target));
            }
        };
        const bool transformed = shape == key_shape::xor3_scalar || shape == key_shape::xor3_simd;
        const bool pointer = shape == key_shape::pointer_scalar || shape == key_shape::pointer_simd;
        emit({0x3d, 0x47, 0x44, 0x50, 0x43, 0xba, 0x20, 0, 0, 0});
        if (shape == key_shape::xor3_simd) {
            emit({0xe8});
            const auto next = base + 0x1000 + cursor - 0x200 + 4;
            u32(static_cast<std::uint32_t>(base + 0x1c00 - next));
        } else
            reference(base + (transformed ? 0x2600 : (pointer ? 0x2100 : 0x2000)));
        if (shape == key_shape::direct_simd || shape == key_shape::pointer_simd)
            emit({0xf3, 0x0f, 0x6f, 0x01, 0xf3, 0x0f, 0x6f, 0x49, 0x10});
        else
            emit({0x0f, 0xb6, 0x01});
        emit({0xc3});
        key_fixture result;
        for (std::size_t i = 0; i < 32; ++i) {
            result.expected[i] = static_cast<std::byte>(i + 1);
            bytes[0x1200 + i] = static_cast<std::uint8_t>(i + 1);
        }
        if (pointer) {
            if (x64)
                patch_u64(bytes, 0x1300, base + 0x2000);
            else
                patch_u32(bytes, 0x1300, static_cast<std::uint32_t>(base + 0x2000));
        }
        if (transformed) {
            for (std::size_t i = 0; i < 32; ++i) {
                const auto a = static_cast<std::uint8_t>(i * 7 + 3), b = static_cast<std::uint8_t>(i * 13 + 11);
                bytes[0x1200 + i] = a;
                bytes[0x1220 + i] = b;
                bytes[0x1240 + i] = static_cast<std::uint8_t>(static_cast<std::uint32_t>(a) ^ b ^ (i + 1));
            }
            cursor = 0xe00;
            reference(base + 0x2600);
            emit({0xba, 0x20, 0, 0, 0});
            reference(base);
            if (shape == key_shape::xor3_scalar) {
                // Three syntactic RVA displacements, two byte XORs, one byte store.
                for (const auto rva : {0x2000U, 0x2020U, 0x2040U}) {
                    emit({0x0f, 0xb6, 0x84, 0x01});
                    u32(rva);
                }
                emit({0x30, 0xc8, 0x30, 0xd0, 0x88, 0x01, 0xc3});
            } else {
                for (const auto rva : {0x2000U, 0x2020U, 0x2040U}) {
                    emit({0xf3, 0x0f, 0x6f, 0x84, 0x01});
                    u32(rva);
                }
                emit({0x0f, 0x57, 0xc1, 0x0f, 0x57, 0xc2, 0xf3, 0x0f, 0x7f, 0x01, 0x83, 0xc1, 0x10, 0xc3});
            }
        }
        result.image.reserve(bytes.size());
        for (auto value : bytes)
            result.image.push_back(static_cast<std::byte>(value));
        return result;
    }
} // namespace test::support
