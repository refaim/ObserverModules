#pragma once

#include "../extractor.h"
#include <fstream>
#include <memory>

namespace extractor
{
    class rgss_parser
    {
    protected:
        static uint32_t read_u32(std::ifstream &stream);

    public:
        virtual ~rgss_parser() = default;
        virtual std::vector<std::unique_ptr<file> > list_files(std::ifstream &stream) = 0;
        virtual archive_info get_archive_info() const = 0;
        
        uint32_t decrypt(uint32_t magic, std::vector<char> &data) const;

    protected:
        virtual uint32_t advance_magic(uint32_t &magic) const = 0;
    };

    class rgssad_parser final : public rgss_parser
    {
    private:
        uint8_t version_;

    protected:
        uint32_t advance_magic(uint32_t &magic) const override;

    public:
        explicit rgssad_parser(uint8_t version);
        std::vector<std::unique_ptr<file> > list_files(std::ifstream &stream) override;
        archive_info get_archive_info() const override;
    };

    class rgss3a_parser final : public rgss_parser
    {
    protected:
        uint32_t advance_magic(uint32_t &magic) const override;

    public:
        std::vector<std::unique_ptr<file> > list_files(std::ifstream &stream) override;
        archive_info get_archive_info() const override;
    };
}