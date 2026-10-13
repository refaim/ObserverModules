#include "../framework/testcase.h"
#include "../support/archive_fixtures.h"

#include <array>
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

TEST_CASE("archives: hermetic native RPG Maker XP RGSSAD", "[integration][hermetic]")
{
    const auto contents = test::support::make_rpgmaker_legacy_archive("Data\\hello.txt", "rpgmaker xp payload");
    const test::support::temporary_archive archive("rpgmaker-xp", contents);
    test::test_archive(archive.path(), {{L"Data\\hello.txt", "rpgmaker xp payload"}});
}

TEST_CASE("archives: hermetic native RPG Maker VX RGSS2A", "[integration][hermetic]")
{
    const auto contents = test::support::make_rpgmaker_legacy_archive("Data\\vx.txt", "rpgmaker vx payload");
    const test::support::temporary_archive archive("rpgmaker-vx", contents);
    test::test_archive(archive.path(), {{L"Data\\vx.txt", "rpgmaker vx payload"}});
}

TEST_CASE("archives: native RPG Maker keeps independent per-entry keys", "[integration][hermetic]")
{
    const std::array entries{
        test::support::rpgmaker_entry{"Data\\one.txt", "first payload"},
        test::support::rpgmaker_entry{"Graphics\\two.bin", std::string(300, '2')},
        test::support::rpgmaker_entry{"Data\\three.txt", "third payload is the longest"},
    };
    const auto contents = test::support::make_rpgmaker_legacy_archive(entries);
    const test::support::temporary_archive archive("rpgmaker-legacy-keys", contents);
    test::test_archive(archive.path(), {{L"Data\\one.txt", "first payload"},
                                        {L"Graphics\\two.bin", std::string(300, '2')},
                                        {L"Data\\three.txt", "third payload is the longest"}});
}

TEST_CASE("archives: native RPG Maker decrypts every word remainder", "[integration][hermetic]")
{
    const std::array entries{
        test::support::rpgmaker_entry{"Data\\tail1.bin", "1"},
        test::support::rpgmaker_entry{"Data\\tail2.bin", "12"},
        test::support::rpgmaker_entry{"Data\\tail3.bin", "123"},
        test::support::rpgmaker_entry{"Data\\tail0.bin", "1234"},
    };
    const auto contents = test::support::make_rpgmaker_legacy_archive(entries);
    const test::support::temporary_archive archive("rpgmaker-legacy-tails", contents);
    test::test_archive(archive.path(), {{L"Data\\tail1.bin", "1"},
                                        {L"Data\\tail2.bin", "12"},
                                        {L"Data\\tail3.bin", "123"},
                                        {L"Data\\tail0.bin", "1234"}});
}

TEST_CASE("archives: native RPG Maker decrypts a payload larger than one buffer", "[integration][hermetic]")
{
    std::string payload(2 * 128 * 1024 + 3, '\0');
    for (std::size_t index = 0; index < payload.size(); ++index) {
        payload[index] = static_cast<char>('A' + static_cast<int>(index % 26));
    }
    const auto contents = test::support::make_rpgmaker_legacy_archive("Data\\large.bin", payload);
    const test::support::temporary_archive archive("rpgmaker-legacy-large", contents);
    test::test_archive(archive.path(), {{L"Data\\large.bin", payload}});
}

TEST_CASE("archives: native RPG Maker decodes an independently written legacy fixture", "[integration][hermetic]")
{
    // Literal bytes derived from the published v1 algorithm and checked against the local
    // RPGMakerDecrypter reference; this does not use the repository fixture encoder.
    const test::support::byte_buffer contents{
        0x52, 0x47, 0x53, 0x53, 0x41, 0x44, 0x00, 0x01, 0xee, 0xca, 0xad, 0xde, 0xb1, 0xd7, 0x89, 0x8f, 0xd9,
        0xca, 0xe4, 0xaa, 0x70, 0xe4, 0x7c, 0xa2, 0x8b, 0xe4, 0xc4, 0xd0, 0x31, 0x67, 0x26, 0xf8, 0x17, 0xb0,
        0x6f, 0xad, 0x62, 0xd2, 0x0c, 0xc9, 0x7a, 0xdc, 0xa4, 0x16, 0xa5, 0xf7, 0x36, 0x25, 0x01, 0xe2, 0x8c,
        0x32, 0x88, 0x0c, 0x14, 0x5a, 0x53, 0x1a, 0x7d, 0x52, 0x0f, 0x20, 0xc3, 0xf4, 0x26, 0x99,
    };
    const test::support::temporary_archive archive("rpgmaker-legacy-literal", contents);
    test::test_archive(archive.path(), {{L"Data\\literal.bin", "abcd"}, {L"Graphics\\literal.txt", "xy"}});
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

TEST_CASE("archives: Zanzarah strips frame bytes from payload", "[integration][hermetic][zanzarah-framing]")
{
    auto contents = test::support::make_zanzarah_archive("a", "hello");
    contents[17] = 13;
    contents[21] = 1;
    contents[22] = 1;
    contents[23] = 0;
    contents[24] = 0;
    contents.push_back(2);
    contents.push_back(2);
    contents.push_back(0);
    contents.push_back(0);
    const test::support::temporary_archive archive("zanzarah-framing", contents);
    test::test_archive(archive.path(), {{L"a", "hello"}});
}
