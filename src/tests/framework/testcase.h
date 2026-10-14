#ifndef UNPACKRAT_TESTS_FRAMEWORK_TESTCASE_H
#define UNPACKRAT_TESTS_FRAMEWORK_TESTCASE_H

#include <filesystem>
#include <string>
#include <vector>

namespace test
{
    struct expected_file final
    {
        std::wstring path;
        std::string contents;
    };

    void test_archive(const std::filesystem::path &path, const std::vector<expected_file> &expected_files);
    void test_external_archive(const std::filesystem::path &path);
} // namespace test
#endif
