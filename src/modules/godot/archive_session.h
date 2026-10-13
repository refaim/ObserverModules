#pragma once

#include "../../core/godot/key_recovery.h"
#include "../../core/godot/pck.h"
#include "../extractor.h"

#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <istream>
#include <memory>
#include <string_view>

namespace unpackrat::godot
{
    struct session_limits final
    {
        std::size_t max_directory_entries = 4096;
        std::size_t max_executables = 16;
        std::uint64_t max_validation_bytes = 64ULL * 1024 * 1024;
        pck_limits index{};
        key_recovery_limits key_scan{};
    };

    // Decision policy for keys which have already passed complete digest verification.
    // Owns and erases its one admitted key; a second distinct verified key is ambiguous.
    class validated_key_selection final
    {
      public:
        validated_key_selection() = default;
        ~validated_key_selection();
        validated_key_selection(const validated_key_selection &) = delete;
        validated_key_selection &operator=(const validated_key_selection &) = delete;
        void admit(const codecs::key256 &key);
        [[nodiscard]] const codecs::key256 *get() const noexcept;

      private:
        codecs::key256 key_{};
        bool present_ = false;
    };

    // One archive lifetime owns its stable source, admitted plaintext directory and verified
    // key. Windows file leases remain inside this module adapter; core readers only borrow
    // standard streams. No runtime is loaded or executed, and no key is written to disk.
    class archive_session final : public extractor::storage_state
    {
      public:
        explicit archive_session(const std::filesystem::path &path, std::string_view password = {},
                                 const session_limits &limits = {}, bool decode_resources = false);
        ~archive_session() override;
        archive_session(const archive_session &) = delete;
        archive_session &operator=(const archive_session &) = delete;

        [[nodiscard]] std::istream &stream() noexcept;
        [[nodiscard]] bool decode_resources() const noexcept;
        [[nodiscard]] const pck_index &index() const noexcept;
        [[nodiscard]] const codecs::key256 *key() const noexcept;
        [[nodiscard]] const std::filesystem::path &source_path() const noexcept;

      private:
        struct state;
        std::unique_ptr<state> state_;
    };
} // namespace unpackrat::godot
