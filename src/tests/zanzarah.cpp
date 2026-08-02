#include <catch2/catch_test_macros.hpp>

#include "framework/testcase.h"

using namespace test;

TEST_CASE("zanzarah: zanzarah1", "[compatibility][.]")
{
    test_external_archive("zanzarah\\zanzarah1.pak");
}

TEST_CASE("zanzarah: zanzarah2", "[compatibility][.]")
{
    test_external_archive("zanzarah\\zanzarah2.pak");
}

TEST_CASE("zanzarah: zanzarah3", "[compatibility][.]")
{
    test_external_archive("zanzarah\\zanzarah3.pak");
}
