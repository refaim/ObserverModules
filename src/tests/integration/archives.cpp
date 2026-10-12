#include "../framework/testcase.h"
#include "../support/archive_fixtures.h"

#include <string>

#include <catch2/catch_test_macros.hpp>

TEST_CASE("archives: hermetic RenPy RPA 2.0", "[integration][hermetic]")
{
    const auto contents = test::support::make_renpy_archive("dir/hello.txt", "renpy payload");
    const test::support::temporary_archive archive("renpy", contents);
    test::test_archive(archive.path(), {{L"dir\\hello.txt", "renpy payload"}});
}

TEST_CASE("archives: hermetic RenPy RPA 3.0", "[integration][hermetic]")
{
    test::support::renpy_archive_options options;
    options.version = test::support::renpy_version::rpa_3_0;
    const auto contents = test::support::make_renpy_archive("dir/encrypted.txt", "encrypted payload", options);
    const test::support::temporary_archive archive("renpy-v3", contents);
    test::test_archive(archive.path(), {{L"dir\\encrypted.txt", "encrypted payload"}});
}

TEST_CASE("archives: RenPy prepends an indexed header", "[integration][hermetic]")
{
    test::support::renpy_archive_options options;
    options.header = "header:";
    const auto contents = test::support::make_renpy_archive("header.txt", "payload", options);
    const test::support::temporary_archive archive("renpy-header", contents);
    test::test_archive(archive.path(), {{L"header.txt", "header:payload"}});
}

TEST_CASE("archives: RenPy accepts an explicit empty header", "[integration][hermetic]")
{
    test::support::renpy_archive_options options;
    options.include_none_header = true;
    const auto contents = test::support::make_renpy_archive("no-header.txt", "payload", options);
    const test::support::temporary_archive archive("renpy-no-header", contents);
    test::test_archive(archive.path(), {{L"no-header.txt", "payload"}});
}

TEST_CASE("archives: hermetic RPG Maker RGSS3A", "[integration][hermetic]")
{
    const auto contents = test::support::make_rpgmaker_archive("Data\\hello.txt", "rpgmaker payload");
    const test::support::temporary_archive archive("rpgmaker", contents);
    test::test_archive(archive.path(), {{L"Data\\hello.txt", "rpgmaker payload"}});
}

TEST_CASE("archives: RPG Maker decrypts a partial final word", "[integration][hermetic]")
{
    const auto contents = test::support::make_rpgmaker_archive("Data\\tail.txt", "tail!");
    const test::support::temporary_archive archive("rpgmaker-tail", contents);
    test::test_archive(archive.path(), {{L"Data\\tail.txt", "tail!"}});
}

TEST_CASE("archives: hermetic Zanzarah PAK", "[integration][hermetic]")
{
    const auto contents = test::support::make_zanzarah_archive("..\\data\\hello.txt", "zanzarah payload");
    const test::support::temporary_archive archive("zanzarah", contents);
    test::test_archive(archive.path(), {{L"data\\hello.txt", "zanzarah payload"}});
}

TEST_CASE("archives: Zanzarah preserves an already-relative path", "[integration][hermetic]")
{
    const auto contents = test::support::make_zanzarah_archive("data\\direct.txt", "direct payload");
    const test::support::temporary_archive archive("zanzarah-relative", contents);
    test::test_archive(archive.path(), {{L"data\\direct.txt", "direct payload"}});
}
