#pragma once

#include <filesystem>
#include <functional>
#include <istream>
#include <memory>
#include <ostream>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace extractor
{
    typedef struct
    {
        uint32_t Data1;
        uint16_t Data2;
        uint16_t Data3;
        uint8_t Data4[8];
    } guid;

    typedef struct
    {
        guid id;
        uint32_t major_version;
        uint32_t minor_version;
    } version_info;

    version_info get_version_info() noexcept;
    void configure(const wchar_t *settings) noexcept;
    void reset_settings() noexcept;

    class read_error final : public std::runtime_error
    {
      public:
        read_error() : runtime_error("")
        {
        }
    };

    class password_required final : public std::runtime_error
    {
      public:
        password_required() : runtime_error("Godot encryption key required")
        {
        }
    };

    // An extractor owns its archive-specific lifetime state. Entry callbacks may borrow this
    // state because archive destroys its file list before releasing the extractor.
    struct storage_state
    {
        virtual ~storage_state() = default;
    };

    struct archive_info final
    {
        std::wstring format;
        std::wstring compression;
        std::wstring comment;
    };

    struct file final
    {
        std::string path;
        std::string header;
        int64_t offset = 0;
        int64_t compressed_body_size_in_bytes = 0;
        int64_t uncompressed_body_size_in_bytes = 0;
        uint32_t magic = 0;
        // Optional custom extraction. When empty the archive performs the default
        // header copy followed by the decrypted raw body copy. When set, the handler
        // receives the open archive input stream, the destination output stream and the
        // progress callback, and is responsible for the whole entry body instead.
        //
        // Handler contract:
        //   * The source stream has failbit and badbit exceptions enabled, so a failed
        //     seek or read surfaces as std::ios_base::failure. The handler must translate
        //     those source stream IO failures into extractor::read_error; the archive
        //     maps extractor::read_error to archive::read_error.
        //   * Output stream IO failures surface as std::ios_base::failure. The handler
        //     must let them escape unchanged; the archive maps any escaping
        //     std::ios_base::failure, including a failed output close, to
        //     archive::write_error.
        //   * Any other exception escapes unchanged after the partial destination is
        //     removed.
        std::function<void(std::istream &, std::ostream &, const std::function<void(int64_t)> &)> extract;
    };

    class extractor
    {
      public:
        virtual ~extractor() = default;

        static std::vector<std::byte> get_signature();

        archive_info get_archive_info(std::istream &stream, const std::span<const std::byte> &data,
                                      const std::filesystem::path &path = {}, std::string_view password = {});

        // archive::prepare_files calls this only after get_archive_info has completed
        // successfully for the current source. Per-archive state is therefore admitted.
        std::vector<std::unique_ptr<file>> list_files(std::istream &stream);

        uint32_t decrypt(uint32_t magic, std::vector<char> &data) const;

        void reset_storage() noexcept
        {
            state_.reset();
        }

      protected:
        std::unique_ptr<storage_state> state_;
    };
} // namespace extractor
