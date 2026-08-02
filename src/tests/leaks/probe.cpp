#include "../../api.h"
#include "../support/archive_fixtures.h"

#include <algorithm>
#include <array>
#include <charconv>
#include <cstdlib>
#include <filesystem>
#include <format>
#include <fstream>
#include <iostream>
#include <iterator>
#include <limits>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

#include <windows.h>

#ifdef _DEBUG
#error "leak-probe must be built as the optimized Release executable"
#endif

namespace
{
    constexpr std::string_view marker_prefix = "OBSERVER_LEAK_PROBE";
    enum class probe_mode : std::uint8_t
    {
        operations,
        lifecycle,
    };

    struct options final
    {
        std::size_t warmup_rounds = 8;
        std::size_t iterations_per_window = 100;
        std::size_t windows = 3;
        probe_mode mode = probe_mode::operations;
        std::string_view scenario = "all";
        bool automatic = false;
    };

    [[nodiscard]] std::size_t parse_count(const std::string_view value, const std::string_view option)
    {
        std::size_t result = 0;
        const auto [end, error] = std::from_chars(value.data(), value.data() + value.size(), result);
        if (error != std::errc{} || end != value.data() + value.size() || result == 0 || result > 1'000'000) {
            throw std::runtime_error(std::format("{} expects an integer from 1 to 1000000", option));
        }
        return result;
    }

    [[nodiscard]] options parse_options(const int argc, char **argv)
    {
        options result;
        for (int index = 1; index < argc; ++index) {
            const std::string_view argument(argv[index]);
            if (argument == "--automatic") {
                result.automatic = true;
                continue;
            }

            if (argument == "--help") {
                std::cout << "Usage: leak-probe.exe [--automatic] [--mode operations|lifecycle] "
                             "[--scenario all|NAME] [--warmup N] [--iterations N] [--windows N]\n";
                std::exit(EXIT_SUCCESS);
            }

            if (argument != "--mode" && argument != "--scenario" && argument != "--warmup" &&
                argument != "--iterations" && argument != "--windows") {
                throw std::runtime_error(std::format("Unknown option: {}", argument));
            }
            if (++index >= argc) {
                throw std::runtime_error(std::format("Missing value after {}", argument));
            }

            if (argument == "--mode") {
                const std::string_view mode(argv[index]);
                if (mode == "operations") {
                    result.mode = probe_mode::operations;
                } else if (mode == "lifecycle") {
                    result.mode = probe_mode::lifecycle;
                } else {
                    throw std::runtime_error("--mode expects operations or lifecycle");
                }
            } else if (argument == "--scenario") {
                result.scenario = argv[index];
            } else if (argument == "--warmup") {
                const auto count = parse_count(argv[index], argument);
                result.warmup_rounds = count;
            } else if (argument == "--iterations") {
                const auto count = parse_count(argv[index], argument);
                result.iterations_per_window = count;
            } else {
                const auto count = parse_count(argv[index], argument);
                result.windows = count;
            }
        }
        return result;
    }

    void suppress_error_dialogs() noexcept
    {
        SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX);
    }

    [[nodiscard]] std::filesystem::path executable_directory()
    {
        std::wstring path(32'768, L'\0');
        const auto length = GetModuleFileNameW(nullptr, path.data(), static_cast<DWORD>(path.size()));
        if (length == 0 || length >= path.size()) {
            throw std::runtime_error("Failed to locate leak-probe.exe");
        }
        path.resize(length);
        return std::filesystem::path(path).parent_path();
    }

    template <typename Function> [[nodiscard]] Function resolve_export(const HMODULE module, const char *name)
    {
        const auto address = GetProcAddress(module, name);
        if (address == nullptr) {
            throw std::runtime_error(std::format("Required module export is absent: {}", name));
        }
#pragma warning(suppress : 4191)
        return reinterpret_cast<Function>(address);
    }

    class storage_handle final
    {
      public:
        storage_handle(const module_cbs &api, const HANDLE value) noexcept : api_(api), value_(value)
        {
        }

        ~storage_handle()
        {
            if (value_ != nullptr) {
                api_.CloseStorage(value_);
            }
        }

        storage_handle(const storage_handle &) = delete;
        storage_handle &operator=(const storage_handle &) = delete;
        storage_handle(storage_handle &&other) noexcept : api_(other.api_), value_(other.value_)
        {
            other.value_ = nullptr;
        }

        [[nodiscard]] HANDLE get() const noexcept
        {
            return value_;
        }

      private:
        const module_cbs &api_;
        HANDLE value_;
    };

    class temporary_output final
    {
      public:
        explicit temporary_output(std::filesystem::path path) : path_(std::move(path))
        {
            std::error_code error;
            std::filesystem::remove(path_, error);
        }

        ~temporary_output()
        {
            std::error_code error;
            std::filesystem::remove(path_, error);
        }

        [[nodiscard]] const std::filesystem::path &path() const noexcept
        {
            return path_;
        }

      private:
        std::filesystem::path path_;
    };

    [[nodiscard]] BOOL CALLBACK report_progress(const HANDLE context, const __int64 bytes_processed) noexcept
    {
        if (context == nullptr || bytes_processed < 0) {
            return FALSE;
        }
        auto &total = *static_cast<__int64 *>(context);
        total += bytes_processed;
        return TRUE;
    }

    class loaded_module final
    {
      public:
        explicit loaded_module(const std::filesystem::path &path)
            : library_(LoadLibraryExW(path.c_str(), nullptr,
                                      LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS))
        {
            if (library_ == nullptr) {
                throw std::runtime_error(
                    std::format("Failed to load {} (Win32 error {})", path.filename().string(), GetLastError()));
            }

            try {
                const auto expected_path = std::filesystem::canonical(path);
                const auto actual_path = std::filesystem::canonical(module_path(library_));
                if (!std::filesystem::equivalent(expected_path, actual_path)) {
                    throw std::runtime_error(std::format("Loaded module path mismatch: expected {}, received {}",
                                                         expected_path.string(), actual_path.string()));
                }

                const auto load = resolve_export<LoadSubModuleFunc>(library_, "LoadSubModule");
                unload_ = resolve_export<UnloadSubModuleFunc>(library_, "UnloadSubModule");

                ModuleLoadParameters parameters{};
                parameters.StructSize = sizeof(parameters);
                if (load(&parameters) == FALSE) {
                    throw std::runtime_error(std::format("LoadSubModule failed for {}", path.filename().string()));
                }
                loaded_ = true;
                api_ = parameters.ApiFuncs;

                if (parameters.ApiVersion != ACTUAL_API_VERSION || api_.OpenStorage == nullptr ||
                    api_.CloseStorage == nullptr || api_.GetItem == nullptr || api_.ExtractItem == nullptr ||
                    api_.PrepareFiles == nullptr) {
                    throw std::runtime_error(
                        std::format("Invalid Observer API table from {}", path.filename().string()));
                }
            } catch (...) {
                release();
                throw;
            }
        }

        ~loaded_module()
        {
            release();
        }

        loaded_module(const loaded_module &) = delete;
        loaded_module &operator=(const loaded_module &) = delete;

        void exercise_success(const std::filesystem::path &archive_path, const std::wstring_view expected_path,
                              const std::string_view expected_payload, const std::filesystem::path &output_path) const
        {
            auto storage = open_archive(archive_path);

            if (api_.PrepareFiles(storage.get()) == FALSE) {
                throw std::runtime_error("PrepareFiles failed for a hermetic fixture");
            }

            StorageItemInfo item{};
            if (api_.GetItem(storage.get(), 0, &item) != GET_ITEM_OK) {
                throw std::runtime_error("GetItem failed for the only expected fixture entry");
            }
            if (std::wstring_view(item.Path) != expected_path || item.Size < 0) {
                throw std::runtime_error("The fixture entry metadata is incorrect");
            }
            StorageItemInfo unexpected_item{};
            if (api_.GetItem(storage.get(), 1, &unexpected_item) != GET_ITEM_NOMOREITEMS) {
                throw std::runtime_error("The fixture unexpectedly contains more than one entry");
            }

            temporary_output output(output_path);
            __int64 progress = 0;
            const ExtractOperationParams extract_parameters{
                .ItemIndex = 0,
                .Flags = 0,
                .DestPath = output.path().c_str(),
                .Password = nullptr,
                .Callbacks = {.signalContext = &progress, .FileProgress = report_progress},
            };
            if (api_.ExtractItem(storage.get(), extract_parameters) != SER_SUCCESS) {
                throw std::runtime_error("ExtractItem failed for a hermetic fixture");
            }

            std::ifstream extracted(output.path(), std::ios::binary);
            if (!extracted.is_open()) {
                throw std::runtime_error("ExtractItem did not create its output file");
            }
            const std::string actual_payload{std::istreambuf_iterator<char>(extracted),
                                             std::istreambuf_iterator<char>()};
            if (actual_payload != expected_payload || static_cast<std::uintmax_t>(item.Size) != actual_payload.size() ||
                progress <= 0) {
                throw std::runtime_error("Extracted fixture data is incorrect");
            }
        }

        void expect_prepare_failure(const std::filesystem::path &archive_path) const
        {
            auto storage = open_archive(archive_path);
            if (api_.PrepareFiles(storage.get()) != FALSE) {
                throw std::runtime_error("PrepareFiles unexpectedly accepted a malformed fixture");
            }
        }

        void expect_extract_status(const std::filesystem::path &archive_path, const std::filesystem::path &output_path,
                                   const ExtractProgressFunc progress_callback, const int expected_status) const
        {
            auto storage = open_archive(archive_path);
            if (api_.PrepareFiles(storage.get()) == FALSE) {
                throw std::runtime_error("PrepareFiles rejected an extraction-status fixture");
            }

            __int64 progress = 0;
            const ExtractOperationParams extract_parameters{
                .ItemIndex = 0,
                .Flags = 0,
                .DestPath = output_path.c_str(),
                .Password = nullptr,
                .Callbacks = {.signalContext = &progress, .FileProgress = progress_callback},
            };
            const auto actual_status = api_.ExtractItem(storage.get(), extract_parameters);
            if (actual_status != expected_status) {
                throw std::runtime_error(
                    std::format("ExtractItem returned {}, expected {}", actual_status, expected_status));
            }
        }

        void expect_entry_count(const std::filesystem::path &archive_path, const std::size_t expected_count) const
        {
            if (expected_count > static_cast<std::size_t>(std::numeric_limits<int>::max())) {
                throw std::runtime_error("Expected entry count is outside the Observer ABI range");
            }

            auto storage = open_archive(archive_path);
            if (api_.PrepareFiles(storage.get()) == FALSE) {
                throw std::runtime_error("PrepareFiles rejected a large valid metadata fixture");
            }
            for (std::size_t index = 0; index < expected_count; ++index) {
                StorageItemInfo item{};
                if (api_.GetItem(storage.get(), static_cast<int>(index), &item) != GET_ITEM_OK || item.Size != 1 ||
                    item.PackedSize != 1 || item.Path[0] == L'\0') {
                    throw std::runtime_error("GetItem returned invalid large-fixture metadata");
                }
            }
            StorageItemInfo unexpected_item{};
            if (api_.GetItem(storage.get(), static_cast<int>(expected_count), &unexpected_item) !=
                GET_ITEM_NOMOREITEMS) {
                throw std::runtime_error("The large metadata fixture contains an unexpected extra entry");
            }
        }

      private:
        [[nodiscard]] static std::filesystem::path module_path(const HMODULE module)
        {
            std::wstring path(32'768, L'\0');
            const auto length = GetModuleFileNameW(module, path.data(), static_cast<DWORD>(path.size()));
            if (length == 0 || length >= path.size()) {
                throw std::runtime_error("Failed to resolve the loaded module path");
            }
            path.resize(length);
            return path;
        }

        [[nodiscard]] storage_handle open_archive(const std::filesystem::path &archive_path) const
        {
            std::ifstream archive(archive_path, std::ios::binary);
            if (!archive.is_open()) {
                throw std::runtime_error("Failed to open a hermetic archive fixture");
            }

            std::vector<char> signature(std::size_t{128} * 1024);
            archive.read(signature.data(), static_cast<std::streamsize>(signature.size()));
            if (archive.bad()) {
                throw std::runtime_error("Failed to read a hermetic archive fixture");
            }

            StorageOpenParams open_parameters{
                .StructSize = sizeof(StorageOpenParams),
                .FilePath = archive_path.c_str(),
                .Password = nullptr,
                .Data = signature.data(),
                .DataSize = static_cast<std::size_t>(archive.gcount()),
            };
            StorageGeneralInfo general_info{};
            HANDLE raw_storage = nullptr;
            if (api_.OpenStorage(open_parameters, &raw_storage, &general_info) != SOR_SUCCESS ||
                raw_storage == nullptr) {
                throw std::runtime_error("OpenStorage rejected its hermetic fixture");
            }
            return storage_handle(api_, raw_storage);
        }

        void release() noexcept
        {
            if (loaded_ && unload_ != nullptr) {
                unload_();
                loaded_ = false;
            }
            unload_ = nullptr;
            if (library_ != nullptr) {
                FreeLibrary(library_);
                library_ = nullptr;
            }
        }

        HMODULE library_ = nullptr;
        bool loaded_ = false;
        UnloadSubModuleFunc unload_ = nullptr;
        module_cbs api_{};
    };

    [[nodiscard]] test::support::byte_buffer bytes(const std::string_view value)
    {
        test::support::byte_buffer result;
        result.reserve(value.size());
        std::ranges::transform(value, std::back_inserter(result), [](const char character) {
            return static_cast<std::uint8_t>(static_cast<unsigned char>(character));
        });
        return result;
    }

    [[nodiscard]] BOOL CALLBACK cancel_progress(HANDLE, __int64) noexcept
    {
        return FALSE;
    }

    struct probe_fixtures final
    {
        static constexpr std::size_t large_entry_count = 4'096;
        static constexpr std::size_t expanded_index_size = 64ULL * 1024 * 1024 + 1;

        probe_fixtures()
            : temporary_directory(std::filesystem::temp_directory_path()),
              renpy_output(temporary_directory /
                           std::format("observer-leak-probe-renpy-{}.tmp", GetCurrentProcessId())),
              rpgmaker_output(temporary_directory /
                              std::format("observer-leak-probe-rpgmaker-{}.tmp", GetCurrentProcessId())),
              zanzarah_output(temporary_directory /
                              std::format("observer-leak-probe-zanzarah-{}.tmp", GetCurrentProcessId())),
              cancellation_output(temporary_directory /
                                  std::format("observer-leak-probe-cancellation-{}.tmp", GetCurrentProcessId())),
              read_failure_output(temporary_directory /
                                  std::format("observer-leak-probe-read-failure-{}.tmp", GetCurrentProcessId())),
              renpy_archive("renpy", test::support::make_renpy_archive("dir/hello.txt", "renpy payload")),
              rpgmaker_archive("rpgmaker", test::support::make_rpgmaker_archive("Data\\hello.txt", "rpgmaker payload")),
              zanzarah_archive("zanzarah",
                               test::support::make_zanzarah_archive("..\\data\\hello.txt", "zanzarah payload")),
              malformed_renpy_archive("malformed-renpy", bytes("RPA-2.0 0000000000000000\n")),
              malformed_rpgmaker_archive("malformed-rpgmaker", bytes(std::string{"RGSSAD\0\3", 8})),
              malformed_zanzarah_archive("malformed-zanzarah", test::support::byte_buffer(8, 0)),
              cancellation_archive("cancellation", test::support::make_rpgmaker_archive(
                                                       "abort.txt", std::string(std::size_t{256} * 1024, 'x'))),
              read_failure_archive("read-failure",
                                   test::support::make_renpy_archive_with_index(test::support::byte_buffer{
                                       '}', 'U', 1, 'x', ']', 'K', 0, 'K', 100, 0x86, 'a', 's', '.'})),
              large_metadata_archive("large-metadata",
                                     test::support::make_zanzarah_archive_with_entries(large_entry_count)),
              expanded_metadata_archive("expanded-metadata",
                                        test::support::make_renpy_archive_with_expanded_index(expanded_index_size))
        {
        }

        std::filesystem::path temporary_directory;
        std::filesystem::path renpy_output;
        std::filesystem::path rpgmaker_output;
        std::filesystem::path zanzarah_output;
        std::filesystem::path cancellation_output;
        std::filesystem::path read_failure_output;
        test::support::temporary_archive renpy_archive;
        test::support::temporary_archive rpgmaker_archive;
        test::support::temporary_archive zanzarah_archive;
        test::support::temporary_archive malformed_renpy_archive;
        test::support::temporary_archive malformed_rpgmaker_archive;
        test::support::temporary_archive malformed_zanzarah_archive;
        test::support::temporary_archive cancellation_archive;
        test::support::temporary_archive read_failure_archive;
        test::support::temporary_archive large_metadata_archive;
        test::support::temporary_archive expanded_metadata_archive;
        test::support::temporary_sparse_renpy_archive sparse_metadata_archive;
    };

    struct module_set final
    {
        explicit module_set(const std::filesystem::path &binary_directory)
            : renpy(binary_directory / "renpy.so"), rpgmaker(binary_directory / "rpgmaker.so"),
              zanzarah(binary_directory / "zanzarah.so")
        {
        }

        loaded_module renpy;
        loaded_module rpgmaker;
        loaded_module zanzarah;
    };

    struct scenario_context final
    {
        const module_set &modules;
        const probe_fixtures &fixtures;
    };

    void exercise_small_success(const scenario_context &context)
    {
        context.modules.renpy.exercise_success(context.fixtures.renpy_archive.path(), L"dir\\hello.txt",
                                               "renpy payload", context.fixtures.renpy_output);
        context.modules.rpgmaker.exercise_success(context.fixtures.rpgmaker_archive.path(), L"Data\\hello.txt",
                                                  "rpgmaker payload", context.fixtures.rpgmaker_output);
        context.modules.zanzarah.exercise_success(context.fixtures.zanzarah_archive.path(), L"data\\hello.txt",
                                                  "zanzarah payload", context.fixtures.zanzarah_output);
    }

    void exercise_malformed(const scenario_context &context)
    {
        context.modules.renpy.expect_prepare_failure(context.fixtures.malformed_renpy_archive.path());
        context.modules.rpgmaker.expect_prepare_failure(context.fixtures.malformed_rpgmaker_archive.path());
        context.modules.zanzarah.expect_prepare_failure(context.fixtures.malformed_zanzarah_archive.path());
    }

    void exercise_cancellation(const scenario_context &context)
    {
        const temporary_output output(context.fixtures.cancellation_output);
        context.modules.rpgmaker.expect_extract_status(context.fixtures.cancellation_archive.path(), output.path(),
                                                       cancel_progress, SER_USERABORT);
    }

    void exercise_read_failure(const scenario_context &context)
    {
        const temporary_output output(context.fixtures.read_failure_output);
        context.modules.renpy.expect_extract_status(context.fixtures.read_failure_archive.path(), output.path(),
                                                    report_progress, SER_ERROR_READ);
    }

    void exercise_write_failure(const scenario_context &context)
    {
        context.modules.rpgmaker.expect_extract_status(context.fixtures.rpgmaker_archive.path(),
                                                       context.fixtures.temporary_directory, report_progress,
                                                       SER_ERROR_WRITE);
    }

    void exercise_large_metadata(const scenario_context &context)
    {
        context.modules.zanzarah.expect_entry_count(context.fixtures.large_metadata_archive.path(),
                                                    probe_fixtures::large_entry_count);
    }

    void exercise_sparse_metadata(const scenario_context &context)
    {
        context.modules.renpy.expect_prepare_failure(context.fixtures.sparse_metadata_archive.path());
        context.modules.renpy.expect_prepare_failure(context.fixtures.expanded_metadata_archive.path());
    }

    struct scenario_definition final
    {
        std::string_view name;
        void (*exercise)(const scenario_context &) = nullptr;
    };

    constexpr std::array scenario_suite{
        scenario_definition{"small-success", exercise_small_success},
        scenario_definition{"malformed", exercise_malformed},
        scenario_definition{"cancellation", exercise_cancellation},
        scenario_definition{"read-failure", exercise_read_failure},
        scenario_definition{"write-failure", exercise_write_failure},
        scenario_definition{"large-metadata", exercise_large_metadata},
        scenario_definition{"sparse-metadata", exercise_sparse_metadata},
    };

    [[nodiscard]] const scenario_definition *select_scenario(const std::string_view name)
    {
        if (name == "all") {
            return nullptr;
        }
        const auto selected = std::ranges::find_if(
            scenario_suite, [name](const scenario_definition &scenario) { return scenario.name == name; });
        if (selected == scenario_suite.end()) {
            throw std::runtime_error(std::format("Unknown leak scenario: {}", name));
        }
        return &*selected;
    }

    void exercise_scenario_suite(const module_set &modules, const probe_fixtures &fixtures,
                                 const scenario_definition *const selected_scenario)
    {
        const scenario_context context{modules, fixtures};
        if (selected_scenario != nullptr) {
            selected_scenario->exercise(context);
            return;
        }
        for (const auto &scenario : scenario_suite) {
            scenario.exercise(context);
        }
    }

    void synchronize_snapshot(const std::string_view label, const std::size_t completed_operations,
                              const bool automatic)
    {
        std::cout << marker_prefix << "|SNAPSHOT|" << label << "|pid=" << GetCurrentProcessId()
                  << "|completed_operations=" << completed_operations << '\n'
                  << std::flush;
        if (automatic) {
            return;
        }

        std::string response;
        if (!std::getline(std::cin, response)) {
            throw std::runtime_error(std::format("Snapshot {} was not acknowledged", label));
        }
        const auto expected = std::format("continue|{}", label);
        if (response != expected) {
            throw std::runtime_error(
                std::format("Expected snapshot acknowledgement '{}', received '{}'", expected, response));
        }
    }

    template <typename Round>
    void run_measurement_windows(const options &settings, Round &&exercise, const std::size_t operations_per_round)
    {
        std::size_t completed_operations = 0;
        for (std::size_t round = 0; round < settings.warmup_rounds; ++round) {
            exercise();
            completed_operations += operations_per_round;
        }
        synchronize_snapshot("baseline", completed_operations, settings.automatic);

        for (std::size_t window = 1; window <= settings.windows; ++window) {
            for (std::size_t iteration = 0; iteration < settings.iterations_per_window; ++iteration) {
                exercise();
                completed_operations += operations_per_round;
            }
            synchronize_snapshot(std::format("window-{}", window), completed_operations, settings.automatic);
        }

        std::cout << marker_prefix << "|DONE|pid=" << GetCurrentProcessId()
                  << "|completed_operations=" << completed_operations << '\n'
                  << std::flush;
    }
} // namespace

int main(const int argc, char **argv)
{
    suppress_error_dialogs();

    try {
        const auto settings = parse_options(argc, argv);
        const auto binary_directory = executable_directory();
        const auto mode_name = settings.mode == probe_mode::operations ? "operations" : "lifecycle";
        const auto *const selected_scenario = select_scenario(settings.scenario);
        const auto operations_per_round = selected_scenario == nullptr ? scenario_suite.size() : std::size_t{1};
        const probe_fixtures fixtures;
        std::cout << marker_prefix << "|READY|pid=" << GetCurrentProcessId() << "|mode=" << mode_name
                  << "|configuration=Release|scenarios=";
        if (selected_scenario != nullptr) {
            std::cout << selected_scenario->name;
        } else {
            for (std::size_t index = 0; index < scenario_suite.size(); ++index) {
                if (index != 0) {
                    std::cout << ',';
                }
                std::cout << scenario_suite[index].name;
            }
        }
        std::cout << '\n' << std::flush;

        if (settings.mode == probe_mode::operations) {
            const module_set modules(binary_directory);
            run_measurement_windows(
                settings,
                [&modules, &fixtures, selected_scenario] {
                    exercise_scenario_suite(modules, fixtures, selected_scenario);
                },
                operations_per_round);
        } else {
            const auto exercise_lifecycle = [&binary_directory, &fixtures, selected_scenario] {
                const module_set modules(binary_directory);
                exercise_scenario_suite(modules, fixtures, selected_scenario);
            };
            run_measurement_windows(settings, exercise_lifecycle, operations_per_round);
        }
        return EXIT_SUCCESS;
    } catch (const std::exception &error) {
        std::cerr << marker_prefix << "|ERROR|" << error.what() << '\n' << std::flush;
        return EXIT_FAILURE;
    } catch (...) {
        std::cerr << marker_prefix << "|ERROR|unknown failure\n" << std::flush;
        return EXIT_FAILURE;
    }
}
