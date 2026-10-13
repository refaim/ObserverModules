#pragma once

#include "audio_types.h"

#include <cstddef>
#include <cstdint>
#include <span>

// QOA ("Quite OK Audio") is Godot's lossy sample format. The decoder is the vendored, unmodified
// reference header at third_party/qoa/qoa.h, pinned to Godot 4.5-stable:
//   godot @ 876b290332ec6f2e6d173d08162a02aa7e6ca46d, thirdparty/misc/qoa.h
//   sha256 ab501e91f4afd18c811e689713e5fd3956030562bf80cda784d304af17133f01
//   MIT (c) 2023 Dominic Szablewski (licenses/qoa.txt); see third_party/qoa/PROVENANCE.json.
// Its types and error codes stay private: qoa_reference.c compiles the C implementation in its own
// translation unit, and qoa_codec.cpp drives the caller-owned-buffer frame API, so this adapter
// exposes only our own pcm_audio/audio_error values and never surfaces qoa_desc across the boundary.
namespace unpackrat::codecs
{
    // Decode a complete QOA stream into interleaved PCM16. The 8-byte file header and every frame
    // are validated before the reference decoder writes into our buffer, so a truncated, oversized,
    // or internally inconsistent stream raises ``audio_error`` instead of reading out of bounds.
    // Only mono and stereo streams are produced, matching the Godot sample phase.
    [[nodiscard]] pcm_audio decode_qoa(std::span<const std::byte> input, const audio_limits &limits = {});

    // Decode one complete frame into caller-owned interleaved storage (at most 5120 frames).
    [[nodiscard]] std::uint32_t decode_qoa_frame(std::span<const std::byte> frame, std::uint32_t channels,
                                                 std::uint32_t sample_rate, std::span<std::int16_t> output);
} // namespace unpackrat::codecs
