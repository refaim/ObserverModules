#include "../../core/codecs/crypto_codec.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <future>
#include <span>
#include <string_view>
#include <vector>

#ifdef _DEBUG
#include <crtdbg.h>
#endif

#include <catch2/catch_test_macros.hpp>

namespace
{
    using namespace unpackrat::codecs;

    std::vector<std::byte> decode_hex(const std::string_view hex)
    {
        const auto digit = [](const char c) { return c <= '9' ? c - '0' : c - 'a' + 10; };
        std::vector<std::byte> result;
        for (std::size_t offset = 0; offset < hex.size(); offset += 2) {
            result.push_back(static_cast<std::byte>((static_cast<unsigned>(digit(hex[offset])) << 4U) |
                                                    static_cast<unsigned>(digit(hex[offset + 1]))));
        }
        return result;
    }

    struct nist_vector final
    {
        key256 key{};
        iv128 iv{};
        std::vector<std::byte> ciphertext =
            decode_hex("dc7e84bfda79164b7ecd8486985d386039ffed143b28b1c832113c6331e5407b");
        std::vector<std::byte> plaintext =
            decode_hex("6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e51");

        nist_vector()
        {
            const auto key_bytes = decode_hex("603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4");
            const auto iv_bytes = decode_hex("000102030405060708090a0b0c0d0e0f");
            std::copy(key_bytes.begin(), key_bytes.end(), key.begin());
            std::copy(iv_bytes.begin(), iv_bytes.end(), iv.begin());
        }
    };

#ifdef _DEBUG
    thread_local bool reject_crypto_copy = false;
    thread_local bool crypto_copy_rejected = false;
    thread_local std::size_t largest_crypto_allocation = 0;

    int __cdecl reject_crypto_copy_allocation(const int kind, void *, const std::size_t size, const int block,
                                              const long, const unsigned char *, const int)
    {
        if (kind == _HOOK_ALLOC && block == _NORMAL_BLOCK) {
            largest_crypto_allocation = (std::max)(largest_crypto_allocation, size);
        }
        if (kind == _HOOK_ALLOC && block == _NORMAL_BLOCK && size == 4096 && reject_crypto_copy) {
            reject_crypto_copy = false;
            crypto_copy_rejected = true;
            return 0;
        }
        return 1;
    }

    class scoped_crypto_copy_failure final
    {
      public:
        explicit scoped_crypto_copy_failure(const bool reject = true)
            : previous_(_CrtSetAllocHook(reject_crypto_copy_allocation))
        {
            crypto_copy_rejected = false;
            largest_crypto_allocation = 0;
            reject_crypto_copy = reject;
        }

        ~scoped_crypto_copy_failure()
        {
            reject_crypto_copy = false;
            static_cast<void>(_CrtSetAllocHook(previous_));
        }

        scoped_crypto_copy_failure(const scoped_crypto_copy_failure &) = delete;
        scoped_crypto_copy_failure &operator=(const scoped_crypto_copy_failure &) = delete;

      private:
        _CRT_ALLOC_HOOK previous_ = nullptr;
    };
#endif
} // namespace

TEST_CASE("crypto adapter: MD5 finish consumes the operation", "[crypto-adapter]")
{
    const auto message = decode_hex("616263");
    const auto expected = decode_hex("900150983cd24fb0d6963f7d28e17f72");
    md5_state hash;
    hash.update({});
    hash.update(message);
    const auto digest = hash.finish();
    CHECK(std::equal(digest.begin(), digest.end(), expected.begin(), expected.end()));
    CHECK_THROWS_AS(hash.finish(), crypto_error);
    CHECK_THROWS_AS(hash.update(message), crypto_error);
    CHECK_THROWS_AS(hash.update({}), crypto_error);

    md5_state empty;
    const auto empty_digest = empty.finish();
    const auto empty_expected = decode_hex("d41d8cd98f00b204e9800998ecf8427e");
    CHECK(std::equal(empty_digest.begin(), empty_digest.end(), empty_expected.begin(), empty_expected.end()));
}

TEST_CASE("crypto adapter: bytewise in-place CFB and reset preserve the NIST vector", "[crypto-adapter]")
{
    const nist_vector sample;
    aes256_cfb128 cipher(sample.key, sample.iv);
    auto actual = sample.ciphertext;
    cipher.decrypt({}, {});
    for (std::size_t index = 0; index < actual.size(); ++index) {
        const auto byte = std::span(actual).subspan(index, 1);
        cipher.decrypt(byte, byte);
    }
    CHECK(actual == sample.plaintext);

    cipher.reset(sample.iv);
    cipher.decrypt(std::span(sample.ciphertext).first(7), std::span(actual).first(7));
    cipher.reset(sample.iv);
    CHECK_THROWS_AS(cipher.decrypt(sample.ciphertext, std::span(actual).first(1)), crypto_error);
    cipher.decrypt(sample.ciphertext, actual);
    CHECK(actual == sample.plaintext);
    auto erased = sample.key;
    erase_key(erased);
    CHECK(std::all_of(erased.begin(), erased.end(), [](const std::byte byte) { return byte == std::byte{0}; }));
}

TEST_CASE("crypto adapter: bounded internal chunks preserve long in-place streams", "[crypto-adapter]")
{
    const nist_vector sample;
    std::vector<std::byte> ciphertext(3 * 4096 + 19);
    for (std::size_t index = 0; index < ciphertext.size(); ++index) {
        ciphertext[index] = sample.ciphertext[index % sample.ciphertext.size()];
    }
    std::vector<std::byte> expected(ciphertext.size());
    aes256_cfb128 reference(sample.key, sample.iv);
    reference.decrypt(ciphertext, expected);
    CHECK(std::equal(sample.plaintext.begin(), sample.plaintext.end(), expected.begin()));

    auto actual = ciphertext;
    aes256_cfb128 incremental(sample.key, sample.iv);
    constexpr std::array<std::size_t, 4> chunks{1, 4095, 17, 4096};
    std::size_t offset = 0;
    std::size_t chunk_index = 0;
    while (offset != actual.size()) {
        const auto count = (std::min)(chunks[chunk_index % chunks.size()], actual.size() - offset);
        const auto chunk = std::span(actual).subspan(offset, count);
        incremental.decrypt(chunk, chunk);
        offset += count;
        ++chunk_index;
    }
    CHECK(actual == expected);

    md5_state whole_hash;
    whole_hash.update(expected);
    md5_state split_hash;
    split_hash.update(std::span(expected).first(4095));
    split_hash.update(std::span(expected).subspan(4095));
    CHECK(split_hash.finish() == whole_hash.finish());
}

TEST_CASE("crypto adapter: independent contexts can run concurrently", "[crypto-adapter]")
{
    const nist_vector sample;
    const auto message = decode_hex("616263");
    const auto expected_digest = decode_hex("900150983cd24fb0d6963f7d28e17f72");
    std::array<std::future<bool>, 8> results;
    for (auto &result : results) {
        result = std::async(std::launch::async, [&] {
            for (std::size_t iteration = 0; iteration < 64; ++iteration) {
                aes256_cfb128 cipher(sample.key, sample.iv);
                auto plaintext = sample.ciphertext;
                cipher.decrypt(plaintext, plaintext);
                md5_state hash;
                hash.update(message);
                const auto digest = hash.finish();
                if (plaintext != sample.plaintext ||
                    !std::equal(digest.begin(), digest.end(), expected_digest.begin(), expected_digest.end())) {
                    return false;
                }
            }
            return true;
        });
    }
    for (auto &result : results) {
        CHECK(result.get());
    }
}

#ifdef _DEBUG
TEST_CASE("crypto adapter: long cipher and hash calls bound backend allocation requests", "[crypto-adapter]")
{
    const nist_vector sample;
    aes256_cfb128 cipher(sample.key, sample.iv);
    md5_state hash;
    std::vector<std::byte> bytes(3 * 4096 + 19);
    for (std::size_t index = 0; index < bytes.size(); ++index) {
        bytes[index] = sample.ciphertext[index % sample.ciphertext.size()];
    }
    {
        const scoped_crypto_copy_failure record_only(false);
        cipher.decrypt(bytes, bytes);
        hash.update(bytes);
    }
    CHECK(largest_crypto_allocation == 4096);
    const auto digest = hash.finish();
    CHECK(std::equal(sample.plaintext.begin(), sample.plaintext.end(), bytes.begin()));
    md5_state reference;
    reference.update(std::span(bytes).first(17));
    reference.update(std::span(bytes).subspan(17));
    CHECK(digest == reference.finish());
}

TEST_CASE("crypto adapter: a backend allocation failure becomes crypto_error", "[crypto-adapter]")
{
    md5_state hash;
    const std::array<std::byte, 4096> input{};
    bool translated = false;
    {
        const scoped_crypto_copy_failure failure;
        try {
            hash.update(input);
        } catch (const crypto_error &) {
            translated = true;
        }
    }
    CHECK(crypto_copy_rejected);
    CHECK(translated);
    CHECK_THROWS_AS(hash.finish(), crypto_error);
    CHECK_THROWS_AS(hash.update({}), crypto_error);

    md5_state recovered;
    const auto digest = recovered.finish();
    const auto expected = decode_hex("d41d8cd98f00b204e9800998ecf8427e");
    CHECK(std::equal(digest.begin(), digest.end(), expected.begin(), expected.end()));
}
#endif
