#include "archive_session.h"

#include "../../core/godot/encrypted_payload.h"

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <fstream>
#include <memory>
#include <optional>
#include <string>
#include <system_error>
#include <tuple>
#include <utility>
#include <vector>

#include <fcntl.h>
#include <io.h>
#include <windows.h>

namespace unpackrat::godot
{
    namespace
    {
        struct handle_closer final
        {
            void operator()(void *handle) const noexcept
            {
                static_cast<void>(CloseHandle(handle));
            }
        };
        using read_handle = std::unique_ptr<void, handle_closer>;

        class source_open_error final : public std::runtime_error
        {
          public:
            using std::runtime_error::runtime_error;
        };

        [[nodiscard]] read_handle acquire_read_lease(const std::filesystem::path &path)
        {
            const HANDLE handle = CreateFileW(path.c_str(), GENERIC_READ, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                                              FILE_ATTRIBUTE_NORMAL, nullptr);
            if (handle == INVALID_HANDLE_VALUE) {
                throw source_open_error("Godot source cannot be opened with a stable read lease");
            }
            return read_handle(handle);
        }

        struct file_closer final
        {
            void operator()(FILE *file) const noexcept
            {
                static_cast<void>(fclose(file));
            }
        };
        using file_owner = std::unique_ptr<FILE, file_closer>;

        class descriptor_owner final
        {
          public:
            explicit descriptor_owner(const int descriptor) noexcept : descriptor_(descriptor)
            {
            }
            ~descriptor_owner()
            {
                if (descriptor_ >= 0) {
                    static_cast<void>(_close(descriptor_));
                }
            }
            descriptor_owner(const descriptor_owner &) = delete;
            descriptor_owner &operator=(const descriptor_owner &) = delete;
            [[nodiscard]] int get() const noexcept
            {
                return descriptor_;
            }
            void release() noexcept
            {
                descriptor_ = -1;
            }

          private:
            int descriptor_;
        };

        [[nodiscard]] file_owner open_leased_file(const std::filesystem::path &path)
        {
            auto lease = acquire_read_lease(path);
            const int adopted = _open_osfhandle(reinterpret_cast<std::intptr_t>(lease.get()), _O_RDONLY | _O_BINARY);
            if (adopted < 0) {
                throw source_open_error("Godot source cannot acquire a CRT descriptor");
            }
            // _open_osfhandle transfers ownership only on success. No throwing operation occurs
            // between that transfer and release of the original native-handle owner.
            std::ignore = lease.release();
            descriptor_owner descriptor(adopted);
            FILE *raw = _fdopen(descriptor.get(), "rb");
            if (raw == nullptr) {
                throw source_open_error("Godot source cannot acquire a CRT stream");
            }
            file_owner file(raw);
            descriptor.release();
            return file;
        }

        struct opened_source final
        {
            file_owner file;
            std::ifstream input;

            explicit opened_source(const std::filesystem::path &path) : file(open_leased_file(path)), input(file.get())
            {
                // The pinned MSVC standard library's FILE* constructor borrows the CRT stream.
                // It never resolves the path again. Declaration order destroys input before the
                // FILE owner, whose fclose releases both CRT descriptor and leased native handle.
                input.exceptions(static_cast<std::ios_base::iostate>(static_cast<unsigned>(std::ios::failbit) |
                                                                     static_cast<unsigned>(std::ios::badbit)));
            }
        };

        struct secret_key final
        {
            codecs::key256 value{};
            secret_key() = default;
            ~secret_key()
            {
                codecs::erase_key(value);
            }
            secret_key(const secret_key &) = delete;
            secret_key &operator=(const secret_key &) = delete;
        };

        struct candidate_set final
        {
            std::vector<codecs::key256> values;
            ~candidate_set()
            {
                for (auto &key : values) {
                    codecs::erase_key(key);
                }
            }
        };

        [[nodiscard]] int hex_digit(const char ch) noexcept
        {
            if (ch >= '0' && ch <= '9') {
                return ch - '0';
            }
            if (ch >= 'a' && ch <= 'f') {
                return ch - 'a' + 10;
            }
            if (ch >= 'A' && ch <= 'F') {
                return ch - 'A' + 10;
            }
            return -1;
        }

        [[nodiscard]] bool parse_key(const std::string_view password, secret_key &key) noexcept
        {
            if (password.size() != key.value.size() * 2) {
                return false;
            }
            for (std::size_t index = 0; index < key.value.size(); ++index) {
                const int high = hex_digit(password[index * 2]);
                const int low = hex_digit(password[index * 2 + 1]);
                if (high < 0 || low < 0) {
                    return false;
                }
                key.value[index] =
                    static_cast<std::byte>((static_cast<unsigned>(high) << 4U) | static_cast<unsigned>(low));
            }
            return true;
        }

        [[nodiscard]] bool has_extension(const std::filesystem::path &path, const std::wstring_view expected)
        {
            const auto extension = path.extension().wstring();
            return CompareStringOrdinal(extension.data(), static_cast<int>(extension.size()), expected.data(),
                                        static_cast<int>(expected.size()), TRUE) == CSTR_EQUAL;
        }

        [[nodiscard]] bool ordinary_file(const std::filesystem::path &path)
        {
            std::error_code error;
            return std::filesystem::is_regular_file(std::filesystem::symlink_status(path, error));
        }

        // Directory work is bounded before sorting or choosing executable candidates. Symlinks
        // are not followed, and only ordinary files directly inside this one directory qualify.
        [[nodiscard]] std::vector<std::filesystem::path> sibling_executables(const std::filesystem::path &pack,
                                                                             const std::filesystem::path &preferred,
                                                                             const session_limits &limits)
        {
            std::vector<std::filesystem::path> paths;
            std::error_code error;
            const auto folder = pack.has_parent_path() ? pack.parent_path() : std::filesystem::path(L".");
            std::filesystem::directory_iterator cursor(folder, error);
            const std::filesystem::directory_iterator end;
            std::size_t visited = 0;
            while (!error && cursor != end) {
                if (visited >= limits.max_directory_entries) {
                    return {};
                }
                ++visited;
                const auto path = cursor->path();
                if (path != preferred && has_extension(path, L".exe") && ordinary_file(path)) {
                    paths.push_back(path);
                }
                cursor.increment(error);
            }
            if (error) {
                return {};
            }
            std::ranges::sort(paths);
            return paths;
        }
    } // namespace

    validated_key_selection::~validated_key_selection()
    {
        codecs::erase_key(key_);
    }

    void validated_key_selection::admit(const codecs::key256 &key)
    {
        if (present_ && key_ != key) {
            throw extractor::password_required();
        }
        key_ = key;
        present_ = true;
    }

    const codecs::key256 *validated_key_selection::get() const noexcept
    {
        return present_ ? &key_ : nullptr;
    }

    struct archive_session::state final
    {
        std::filesystem::path path;
        std::unique_ptr<opened_source> source;
        pck_index index;
        validated_key_selection accepted_key;
        bool decode_resources;

        state(const std::filesystem::path &requested_path, const std::string_view password,
              const session_limits &limits, const bool decode)
            : path(requested_path), source(std::make_unique<opened_source>(path)), decode_resources(decode)
        {
            std::optional<pack_location> location = locate_pack(source->input);
            if (!location && has_extension(requested_path, L".exe")) {
                auto sidecar = requested_path;
                sidecar.replace_extension(L".pck");
                source = std::make_unique<opened_source>(sidecar);
                path = std::move(sidecar);
                location = locate_pack(source->input);
            }
            if (!location) {
                throw pck_error("No Godot pack found in this source");
            }

            std::uint64_t directory_validation_bytes = 0;
            bool directory_requires_key = false;
            try {
                index = read_pack(source->input, *location, limits.index);
            } catch (const pck_key_required &required) {
                directory_requires_key = true;
                directory_validation_bytes = required.validation_bytes;
            }
            const pck_entry *member = nullptr;
            if (!directory_requires_key) {
                for (const auto &entry : index.entries) {
                    if (entry.kind == pck_entry_kind::file && entry.encrypted && entry.logical_size > 0 &&
                        (member == nullptr || entry.body.size < member->body.size)) {
                        member = &entry;
                    }
                }
                if (member == nullptr) {
                    return;
                }
            }

            std::uint64_t validation_left = limits.max_validation_bytes;
            const auto verify_candidate = [&](const codecs::key256 &candidate) {
                const std::uint64_t charge = directory_requires_key ? directory_validation_bytes : member->body.size;
                if (charge > validation_left) {
                    return false;
                }
                validation_left -= charge;
                try {
                    pck_index candidate_index;
                    if (directory_requires_key) {
                        candidate_index = read_pack(source->input, *location, limits.index, &candidate);
                    } else {
                        const auto payload = read_encrypted_payload(source->input, member->body);
                        verified_payload_source decoded(source->input, payload, candidate);
                        decoded.verify();
                    }
                    accepted_key.admit(candidate);
                    if (directory_requires_key) {
                        index = std::move(candidate_index);
                    }
                    return true;
                } catch (const key_mismatch &) {
                    return false;
                }
            };

            secret_key supplied;
            if (parse_key(password, supplied) && verify_candidate(supplied.value)) {
                return;
            }

            auto preferred = requested_path;
            preferred.replace_extension(L".exe");
            if (has_extension(requested_path, L".exe")) {
                preferred = requested_path;
            }
            key_recovery_budget scan_budget;
            std::size_t executables = 0;
            candidate_set seen;
            // The recovery budget bounds candidates across every runtime by this hard cap.
            // Reserve before admitting secrets so no growth frees an unwiped key copy.
            seen.values.reserve(max_recovered_keys);
            const auto scan_executable = [&](const std::filesystem::path &executable) {
                if (executables >= limits.max_executables || !ordinary_file(executable)) {
                    return;
                }
                ++executables;
                candidate_set found;
                try {
                    opened_source runtime(executable);
                    found.values = recover_key_candidates(runtime.input, scan_budget, limits.key_scan);
                } catch (const source_open_error &) {
                    return;
                } catch (const key_recovery_error &) {
                    return;
                }
                for (const auto &candidate : found.values) {
                    if (std::find(seen.values.begin(), seen.values.end(), candidate) != seen.values.end()) {
                        continue;
                    }
                    seen.values.push_back(candidate);
                    static_cast<void>(verify_candidate(candidate));
                }
            };
            scan_executable(preferred);
            for (const auto &executable : sibling_executables(path, preferred, limits)) {
                scan_executable(executable);
            }
            if (accepted_key.get() == nullptr) {
                throw extractor::password_required();
            }
        }
    };

    archive_session::archive_session(const std::filesystem::path &path, const std::string_view password,
                                     const session_limits &limits, const bool decode_resources)
        : state_(std::make_unique<state>(path, password, limits, decode_resources))
    {
    }
    archive_session::~archive_session() = default;
    std::istream &archive_session::stream() noexcept
    {
        return state_->source->input;
    }
    bool archive_session::decode_resources() const noexcept
    {
        return state_->decode_resources;
    }
    const pck_index &archive_session::index() const noexcept
    {
        return state_->index;
    }
    const codecs::key256 *archive_session::key() const noexcept
    {
        return state_->accepted_key.get();
    }
    const std::filesystem::path &archive_session::source_path() const noexcept
    {
        return state_->path;
    }
} // namespace unpackrat::godot
