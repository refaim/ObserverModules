#include "../../modules/godot/virtual_tree.h"

#include <cstddef>
#include <optional>
#include <set>
#include <span>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include <catch2/catch_test_macros.hpp>

namespace
{
    using unpackrat::godot::conversion_hint;
    using unpackrat::godot::import_mapping;
    using unpackrat::godot::pck_entry;
    using unpackrat::godot::pck_index;
    using unpackrat::godot::tree_error;
    using unpackrat::godot::tree_limits;
    using unpackrat::godot::virtual_entry;
    using unpackrat::godot::virtual_tree;

    pck_index make_index(const std::vector<std::string> &paths)
    {
        pck_index index;
        index.format_version = 4;
        for (const std::string &path : paths) {
            pck_entry entry;
            entry.path = path;
            entry.body = {0, 0};
            index.entries.push_back(std::move(entry));
        }
        return index;
    }

    virtual_tree build(const pck_index &index, const std::vector<import_mapping> &mappings = {},
                       const std::vector<conversion_hint> &hints = {}, const tree_limits &limits = {})
    {
        return unpackrat::godot::make_virtual_tree(index, mappings, hints, limits);
    }

    const virtual_entry *find(const virtual_tree &tree, const std::string_view path)
    {
        for (const virtual_entry &entry : tree.entries) {
            if (entry.path == path) {
                return &entry;
            }
        }
        return nullptr;
    }

    std::size_t count(const virtual_tree &tree, const std::string_view path)
    {
        std::size_t total = 0;
        for (const virtual_entry &entry : tree.entries) {
            if (entry.path == path) {
                ++total;
            }
        }
        return total;
    }

    std::string with_byte(const std::string_view prefix, const unsigned char byte)
    {
        std::string text(prefix);
        text.push_back(static_cast<char>(byte));
        return text;
    }

    bool valid_utf8(const std::string_view text)
    {
        std::size_t index = 0;
        while (index < text.size()) {
            const unsigned char lead = static_cast<unsigned char>(text[index]);
            std::size_t extra = 0;
            if (lead < 0x80U) {
                extra = 0;
            } else if ((lead & 0xE0U) == 0xC0U && lead >= 0xC2U) {
                extra = 1;
            } else if ((lead & 0xF0U) == 0xE0U) {
                extra = 2;
            } else if ((lead & 0xF8U) == 0xF0U && lead <= 0xF4U) {
                extra = 3;
            } else {
                return false;
            }
            if (index + extra >= text.size()) {
                return false;
            }
            for (std::size_t offset = 1; offset <= extra; ++offset) {
                if ((static_cast<unsigned char>(text[index + offset]) & 0xC0U) != 0x80U) {
                    return false;
                }
            }
            index += extra + 1;
        }
        return true;
    }

    int hex_value(const char digit)
    {
        if (digit >= '0' && digit <= '9') {
            return digit - '0';
        }
        if (digit >= 'A' && digit <= 'F') {
            return digit - 'A' + 10;
        }
        return -1;
    }

    std::string unescape_field(const std::string_view field)
    {
        std::string out;
        for (std::size_t index = 0; index < field.size(); ++index) {
            if (field[index] == '%' && index + 2 < field.size()) {
                const int high = hex_value(field[index + 1]);
                const int low = hex_value(field[index + 2]);
                if (high >= 0 && low >= 0) {
                    out.push_back(static_cast<char>((high << 4) | low));
                    index += 2;
                    continue;
                }
            }
            out.push_back(field[index]);
        }
        return out;
    }

    const virtual_entry *manifest_of(const virtual_tree &tree)
    {
        for (const virtual_entry &entry : tree.entries) {
            if (!entry.source_index.has_value()) {
                return &entry;
            }
        }
        return nullptr;
    }

    std::vector<std::string> tab_fields(const std::string_view row)
    {
        std::vector<std::string> fields;
        std::string current;
        for (const char character : row) {
            if (character == '\t') {
                fields.push_back(std::move(current));
                current.clear();
                continue;
            }
            current.push_back(character);
        }
        fields.push_back(std::move(current));
        return fields;
    }

    std::string repeat(const std::string_view unit, const std::size_t count)
    {
        std::string out;
        out.reserve(unit.size() * count);
        for (std::size_t index = 0; index < count; ++index) {
            out += unit;
        }
        return out;
    }

    std::vector<std::string> fixed_width_names(const std::size_t count)
    {
        std::vector<std::string> names;
        names.reserve(count);
        for (std::size_t index = 0; index < count; ++index) {
            std::string digits = std::to_string(index);
            names.push_back("f" + std::string(6 - digits.size(), '0') + digits);
        }
        return names;
    }
} // namespace

TEST_CASE("godot virtual tree: ordinary files appear exactly once in their original folders")
{
    const virtual_tree tree = build(make_index({"a.txt", "dir/b.png", "c"}));
    REQUIRE(tree.entries.size() == 3);
    CHECK(tree.entries[0].path == "a.txt");
    CHECK(tree.entries[1].path == "dir/b.png");
    CHECK(tree.entries[2].path == "c");
    for (std::size_t index = 0; index < tree.entries.size(); ++index) {
        CHECK(tree.entries[index].source_index == index);
        CHECK(!(tree.entries[index].converted));
        CHECK(tree.entries[index].generated_text.empty());
    }
}

TEST_CASE("godot virtual tree: an empty archive yields an empty tree")
{
    const virtual_tree tree = build(make_index({}));

    CHECK(tree.entries.empty());
}

TEST_CASE("godot virtual tree: import and remap metadata remain reachable exactly once")
{
    const virtual_tree tree = build(make_index({"art/x.png.import", "art/y.ctex.remap", "art/z.png"}));
    REQUIRE(tree.entries.size() == 3);
    CHECK(find(tree, "art/x.png.import")->source_index == 0U);
    CHECK(find(tree, "art/y.ctex.remap")->source_index == 1U);
    CHECK(find(tree, "art/z.png")->source_index == 2U);
}

TEST_CASE("godot virtual tree: a present compiled target with a hint becomes a converted alias")
{
    const pck_index index = make_index({".godot/imported/pic-h.ctex"});
    const std::vector<import_mapping> mappings = {{"art/pic.png", ".godot/imported/pic-h.ctex", ""}};
    const std::vector<conversion_hint> hints = {{0, ".png"}};

    const virtual_tree tree = build(index, mappings, hints);

    REQUIRE(tree.entries.size() == 2);
    CHECK(tree.entries[0].path == "art/pic.png");
    CHECK(tree.entries[0].converted);
    CHECK(manifest_of(tree) != nullptr);

    const virtual_entry *alias = find(tree, "art/pic.png");
    REQUIRE(alias != nullptr);
    CHECK(alias->source_index == 0U);
    CHECK(alias->converted);

    // The compiled target itself is represented by the alias, not a second normal entry.
    CHECK(count(tree, ".godot/imported/pic-h.ctex") == 0);
}

TEST_CASE("godot virtual tree: an alias without a hint keeps the truthful compiled extension")
{
    const pck_index index = make_index({".godot/imported/pic-h.ctex"});
    const std::vector<import_mapping> mappings = {{"art/pic.png", ".godot/imported/pic-h.ctex", ""}};

    const virtual_tree tree = build(index, mappings);

    const virtual_entry *alias = find(tree, "art/pic.ctex");
    REQUIRE(alias != nullptr);
    CHECK(alias->source_index == 0U);
    CHECK(!(alias->converted));
    CHECK(find(tree, "art/pic.png") == nullptr);
}

TEST_CASE("godot virtual tree: feature variants are preserved with deterministic suffixes")
{
    const pck_index index =
        make_index({".godot/imported/pic-h.ctex", ".godot/imported/pic-s3tc.ctex", ".godot/imported/pic-etc2.ctex"});
    const std::vector<import_mapping> mappings = {{"art/pic.png", ".godot/imported/pic-h.ctex", ""},
                                                  {"art/pic.png", ".godot/imported/pic-s3tc.ctex", "s3tc"},
                                                  {"art/pic.png", ".godot/imported/pic-etc2.ctex", "etc2"}};
    const std::vector<conversion_hint> hints = {{0, ".png"}, {1, ".png"}, {2, ".png"}};

    const virtual_tree tree = build(index, mappings, hints);

    REQUIRE(find(tree, "art/pic.png") != nullptr);
    CHECK(find(tree, "art/pic.png")->source_index == 0U);
    const virtual_entry *s3tc = find(tree, "art/pic.s3tc.png");
    REQUIRE(s3tc != nullptr);
    CHECK(s3tc->source_index == 1U);
    CHECK(s3tc->converted);
    const virtual_entry *etc2 = find(tree, "art/pic.etc2.png");
    REQUIRE(etc2 != nullptr);
    CHECK(etc2->source_index == 2U);
}

TEST_CASE("godot virtual tree: a missing target creates no alias and hides nothing")
{
    const pck_index index = make_index({"art/loose.txt"});
    const std::vector<import_mapping> mappings = {{"art/gone.png", ".godot/imported/gone.ctex", ""}};

    const virtual_tree tree = build(index, mappings);

    CHECK(find(tree, "art/gone.png") == nullptr);
    CHECK(find(tree, "art/gone.ctex") == nullptr);
    REQUIRE(find(tree, "art/loose.txt") != nullptr);
    CHECK(find(tree, "art/loose.txt") != nullptr);
}

TEST_CASE("godot virtual tree: an unmapped physical file keeps a supported hinted extension")
{
    const pck_index index = make_index({"art/pic.png"});
    const std::vector<conversion_hint> hints = {{0, ".webp"}};

    const virtual_tree tree = build(index, {}, hints);

    const virtual_entry *entry = find(tree, "art/pic.webp");
    REQUIRE(entry != nullptr);
    CHECK(entry->source_index == 0U);
    CHECK(entry->converted);
}

TEST_CASE("godot virtual tree: rejects invalid conversion hints")
{
    const pck_index index = make_index({"a.txt"});

    CHECK_THROWS_AS(build(index, {}, {{1, ".png"}}), tree_error);
    CHECK_THROWS_AS(build(index, {}, {{0, ".bmp"}}), tree_error);
    CHECK_THROWS_AS(build(index, {}, {{0, ".png"}, {0, ".webp"}}), tree_error);

    const virtual_tree tree = build(index, {}, {{0, ".png"}, {0, ".png"}});
    CHECK(find(tree, "a.png") != nullptr);
}

TEST_CASE("godot virtual tree: accepts every supported hint extension")
{
    const pck_index index =
        make_index({"a.bin", "b.bin", "c.bin", "d.bin", "e.bin", "f.bin", "g.bin", "h.bin", "i.bin", "j.bin"});
    const std::vector<conversion_hint> hints = {{0, ".png"}, {1, ".webp"}, {2, ".wav"}, {3, ".mp3"},  {4, ".ogg"},
                                                {5, ".ttf"}, {6, ".otf"},  {7, ".ttc"}, {8, ".woff"}, {9, ".woff2"}};

    const virtual_tree tree = build(index, {}, hints);

    CHECK(find(tree, "a.png") != nullptr);
    CHECK(find(tree, "b.webp") != nullptr);
    CHECK(find(tree, "c.wav") != nullptr);
    CHECK(find(tree, "d.mp3") != nullptr);
    CHECK(find(tree, "e.ogg") != nullptr);
    CHECK(find(tree, "f.ttf") != nullptr);
    CHECK(find(tree, "g.otf") != nullptr);
    CHECK(find(tree, "h.ttc") != nullptr);
    CHECK(find(tree, "i.woff") != nullptr);
    CHECK(find(tree, "j.woff2") != nullptr);
}

TEST_CASE("godot virtual tree: rejects invalid mapping paths")
{
    const pck_index index = make_index({".godot/imported/x.ctex"});

    CHECK_THROWS_AS(build(index, {{"", ".godot/imported/x.ctex", ""}}), tree_error);
    CHECK_THROWS_AS(build(index, {{"art/a.png", "", ""}}), tree_error);
    CHECK_THROWS_AS(build(index, {{"/art/a.png", ".godot/imported/x.ctex", ""}}), tree_error);
    CHECK_THROWS_AS(build(index, {{"art/../a.png", ".godot/imported/x.ctex", ""}}), tree_error);
    CHECK_THROWS_AS(build(index, {{"art/a:b.png", ".godot/imported/x.ctex", ""}}), tree_error);
    CHECK_THROWS_AS(build(index, {{with_byte("art/a", 0x01U) + ".png", ".godot/imported/x.ctex", ""}}), tree_error);
    CHECK_THROWS_AS(build(index, {{with_byte("art/a", 0x7FU) + ".png", ".godot/imported/x.ctex", ""}}), tree_error);
    CHECK_THROWS_AS(build(index, {{".", ".godot/imported/x.ctex", ""}}), tree_error);
}

TEST_CASE("godot virtual tree: normalizes res://, backslashes and dot segments in mappings")
{
    const pck_index index = make_index({".godot/imported/x.ctex"});
    const std::vector<import_mapping> mappings = {{"res://art/./we\\ird.png", ".godot/imported/x.ctex", ""}};

    const virtual_tree tree = build(index, mappings);

    REQUIRE(find(tree, "art/we/ird.ctex") != nullptr);
    CHECK(find(tree, "art/we/ird.ctex")->source_index == 0U);
}

TEST_CASE("godot virtual tree: mapping normalization drops empty segments")
{
    const pck_index index = make_index({".godot/imported/x.ctex"});
    const std::vector<import_mapping> mappings = {{"art//b.png", ".godot/imported/x.ctex", ""}};

    const virtual_tree tree = build(index, mappings);

    REQUIRE(find(tree, "art/b.ctex") != nullptr);
    CHECK(find(tree, "art/b.ctex")->source_index == 0U);
}

TEST_CASE("godot virtual tree: a mapping with a leading separator is absolute and rejected")
{
    const pck_index index = make_index({".godot/imported/x.ctex"});

    CHECK_THROWS_AS(build(index, {{"\\abs\\x.png", ".godot/imported/x.ctex", ""}}), tree_error);
}

TEST_CASE("godot virtual tree: a mapping whose traversal is the final segment is rejected")
{
    const pck_index index = make_index({".godot/imported/x.ctex"});

    CHECK_THROWS_AS(build(index, {{"art/..", ".godot/imported/x.ctex", ""}}), tree_error);
}

TEST_CASE("godot virtual tree: a compiled target without an extension yields an extensionless alias")
{
    const pck_index index = make_index({".godot/noext"});
    const std::vector<import_mapping> mappings = {{"art/noext.png", ".godot/noext", ""}};

    const virtual_tree tree = build(index, mappings);

    REQUIRE(find(tree, "art/noext") != nullptr);
    CHECK(find(tree, "art/noext")->source_index == 0U);
}

TEST_CASE("godot virtual tree: alias stems handle missing, dotted and leading-dot names")
{
    const pck_index index = make_index({".godot/a.ctex", ".godot/b.ctex", ".godot/c.ctex"});
    const std::vector<import_mapping> mappings = {{"art/thing", ".godot/a.ctex", ""},
                                                  {"x.y/name.dat", ".godot/b.ctex", ""},
                                                  {".hidden", ".godot/c.ctex", ""},
                                                  {"d.b/e", ".godot/a.ctex", ""}};

    const virtual_tree tree = build(index, mappings);

    CHECK(find(tree, "art/thing.ctex") != nullptr);
    CHECK(find(tree, "x.y/name.ctex") != nullptr);
    CHECK(find(tree, ".hidden.ctex") != nullptr);
    CHECK(find(tree, "d.b/e.ctex") == nullptr);
    CHECK(find(tree, "art/thing.ctex")->source_index == 0U);
}

TEST_CASE("godot virtual tree: duplicate physical and logical names are suffixed, never dropped")
{
    const virtual_tree tree = build(make_index({"art/a.txt", "art/a.txt"}));

    CHECK(find(tree, "art/a.txt") != nullptr);
    CHECK(find(tree, "art/a~1.txt") != nullptr);
    CHECK(find(tree, "art/a.txt")->source_index == 0U);
    CHECK(find(tree, "art/a~1.txt")->source_index == 1U);

    CHECK(find(tree, "art/a.txt") != nullptr);
    CHECK(find(tree, "art/a~1.txt") != nullptr);
}

TEST_CASE("godot virtual tree: case-insensitive collisions use native ordinal comparison")
{
    const virtual_tree tree = build(make_index({"A.txt", "a.txt"}));

    CHECK(find(tree, "A.txt") != nullptr);
    CHECK(find(tree, "a~1.txt") != nullptr);
    CHECK(find(tree, "A.txt") != nullptr);
    CHECK(find(tree, "a~1.txt") != nullptr);
}

TEST_CASE("godot virtual tree: a file may not clobber a directory that owns children")
{
    const virtual_tree tree = build(make_index({"a", "a/b"}));

    CHECK(find(tree, "a/b") != nullptr);
    REQUIRE(find(tree, "a~1") != nullptr);
    CHECK(find(tree, "a~1")->source_index == 0U);

    CHECK(find(tree, "a/b") != nullptr);
    CHECK(find(tree, "a~1") != nullptr);
}

TEST_CASE("godot virtual tree: the old service-directory name is an ordinary source")
{
    const virtual_tree tree = build(make_index({"__originals"}));
    REQUIRE(tree.entries.size() == 1);
    CHECK(tree.entries[0].path == "__originals");
    CHECK(tree.entries[0].source_index == 0U);
}

TEST_CASE("godot virtual tree: raw and converted names collide safely")
{
    const virtual_tree tree = build(make_index({"art/a.ctex", "art/A.png"}), {}, {{0, ".png"}});
    CHECK(find(tree, "art/a.ctex") == nullptr);
    CHECK(find(tree, "art/A.png")->source_index == 1U);
    const virtual_entry *converted = find(tree, "art/a~1.png");
    REQUIRE(converted != nullptr);
    CHECK(converted->source_index == 0U);
    CHECK(converted->converted);
}

TEST_CASE("godot virtual tree: escapes unsafe ASCII, percent, control, trailing and device names")
{
    const virtual_tree tree =
        build(make_index({"a<>:\"|?*z.txt", "x.", "y ", "con", "CON.txt", "nul.dat", with_byte("de", 0x7FU) + "l"}));

    CHECK(find(tree, "a%3C%3E%3A%22%7C%3F%2Az.txt") != nullptr);
    CHECK(find(tree, "x%2E") != nullptr);
    CHECK(find(tree, "y%20") != nullptr);
    CHECK(find(tree, "%63on") != nullptr);
    CHECK(find(tree, "%43ON.txt") != nullptr);
    CHECK(find(tree, "%6Eul.dat") != nullptr);
    CHECK(find(tree, "de%7Fl") != nullptr);

    // Every escaped name is recorded in the name manifest.
    REQUIRE(find(tree, "name-map.tsv") != nullptr);
    CHECK(!(find(tree, "name-map.tsv")->source_index.has_value()));
}

TEST_CASE("godot virtual tree: dot components and an empty name become placeholders")
{
    const virtual_tree tree = build(make_index({"a/./b", "", "a/../c"}));

    CHECK(find(tree, "a/%2E/b") != nullptr);
    CHECK(find(tree, "%00") != nullptr);
    CHECK(find(tree, "a/%2E%2E/c") != nullptr);
}

TEST_CASE("godot virtual tree: invalid UTF-8 names escape every byte")
{
    const virtual_tree tree = build(make_index({with_byte("pic", 0xFFU) + ".bin"}));

    CHECK(find(tree, "%70%69%63%FF%2E%62%69%6E") != nullptr);
    REQUIRE(find(tree, "name-map.tsv") != nullptr);
}

TEST_CASE("godot virtual tree: control, DEL, percent and separator bytes are escaped in fields")
{
    const virtual_tree tree = build(make_index({std::string("a\tb%c")}));
    const virtual_tree backslash = build(make_index({std::string("x\\y")}));

    REQUIRE(find(tree, "name-map.tsv") != nullptr);
    const std::string row = find(tree, "name-map.tsv")->generated_text;
    CHECK(row == "0\ta%09b%25c\ta%2509b%2525c\n");

    CHECK(find(backslash, "x/y") != nullptr);
    REQUIRE(manifest_of(backslash) != nullptr);
    CHECK(manifest_of(backslash)->generated_text == "0\tx\\y\tx/y\n");
}

TEST_CASE("godot virtual tree: variant fragments containing separators stay single components")
{
    const pck_index index = make_index({".godot/a.ctex", ".godot/b.ctex"});
    const std::vector<import_mapping> mappings = {
        {"art/we.png", ".godot/a.ctex", "we/ird"},
        {"art/no.png", ".godot/b.ctex", "ba\\ck"},
    };

    const virtual_tree tree = build(index, mappings);

    CHECK(find(tree, "art/we.we%2Fird.ctex") != nullptr);
    CHECK(find(tree, "art/no.ba%5Cck.ctex") != nullptr);
}

TEST_CASE("godot virtual tree: long presented paths fall back to a short index name")
{
    tree_limits limits;
    limits.max_path_chars = 15;

    const virtual_tree tree = build(make_index({"quite/a/fairly/long/path.txt"}), {}, {}, limits);

    CHECK(find(tree, "0") != nullptr);
    CHECK(find(tree, "0")->source_index == 0U);
    REQUIRE(manifest_of(tree) != nullptr);
    CHECK(manifest_of(tree)->path == "name-map.tsv");
    const std::string row = manifest_of(tree)->generated_text;
    CHECK(row == "0\tquite/a/fairly/long/path.txt\t0\n");
}

TEST_CASE("godot virtual tree: a too-tight preferred suffix budget falls through to the fallback name")
{
    tree_limits limits;
    limits.max_path_chars = 15;

    const virtual_tree tree = build(make_index({"abcdefghij.txt", "abcdefghij.txt"}), {}, {}, limits);

    CHECK(find(tree, "abcdefghij.txt") != nullptr);
    CHECK(find(tree, "1") != nullptr);
    CHECK(find(tree, "1")->source_index == 1U);
}

TEST_CASE("godot virtual tree: a long logical alias also falls back to a root index name")
{
    tree_limits limits;
    limits.max_path_chars = 15;

    const pck_index index = make_index({".godot/x.ctex"});
    const std::vector<import_mapping> mappings = {{"deep/very/long/logical/path/name.png", ".godot/x.ctex", ""}};

    const virtual_tree tree = build(index, mappings, {}, limits);

    CHECK(find(tree, "deep/very/long/logical/path/name.ctex") == nullptr);
    REQUIRE(find(tree, "0") != nullptr);
    CHECK(find(tree, "0")->source_index == 0U);
}

TEST_CASE("godot virtual tree: fails the whole tree when a budget is exceeded")
{
    const pck_index index = make_index({"a.txt", "b.txt"});

    tree_limits entries;
    entries.max_entries = 1;
    CHECK_THROWS_AS(build(index, {}, {}, entries), tree_error);

    tree_limits metadata;
    metadata.max_metadata_bytes = 3;
    CHECK_THROWS_AS(build(index, {}, {}, metadata), tree_error);

    tree_limits path;
    path.max_path_chars = 0;
    CHECK_THROWS_AS(build(make_index({"a"}), {}, {}, path), tree_error);
}

TEST_CASE("godot virtual tree: multiple mappings may alias one shared compiled target")
{
    const pck_index index = make_index({".godot/shared.ctex"});
    const std::vector<import_mapping> mappings = {{"art/first.png", ".godot/shared.ctex", ""},
                                                  {"art/second.png", ".godot/shared.ctex", ""}};
    const std::vector<conversion_hint> hints = {{0, ".png"}};

    const virtual_tree tree = build(index, mappings, hints);

    REQUIRE(find(tree, "art/first.png") != nullptr);
    REQUIRE(find(tree, "art/second.png") != nullptr);
    CHECK(find(tree, "art/first.png")->source_index == 0U);
    CHECK(find(tree, "art/second.png")->source_index == 0U);
    CHECK(find(tree, "art/first.png")->converted);
    CHECK(find(tree, "art/first.ctex") == nullptr);
    CHECK(find(tree, "art/second.ctex") == nullptr);
    std::size_t originals = 0;
    for (const auto &row : tree.entries) {
        if (row.source_index == 0U && !row.converted) {
            ++originals;
        }
    }
    CHECK(originals == 0);
}

TEST_CASE("godot virtual tree: serves a span-typed mapping view")
{
    const pck_index index = make_index({".godot/a.ctex"});
    const std::vector<import_mapping> storage = {{"art/a.png", ".godot/a.ctex", ""}};
    const std::span<const import_mapping> view(storage);

    const virtual_tree tree = unpackrat::godot::make_virtual_tree(index, view, {});

    CHECK(find(tree, "art/a.ctex") != nullptr);
}

TEST_CASE("godot virtual tree: leading separators are escaped into relative names and recorded")
{
    const virtual_tree tree = build(make_index({"/leading"}));
    CHECK(find(tree, "leading") != nullptr);
    REQUIRE(find(tree, "name-map.tsv") != nullptr);
}

TEST_CASE("godot virtual tree: the manifest never collides with a raw directory ancestor")
{
    const virtual_tree tree = build(make_index({"name-map.tsv/child", "a?"}));

    // Every physical source index survives.
    const virtual_entry *child = find(tree, "name-map.tsv/child");
    REQUIRE(child != nullptr);
    CHECK(child->source_index == 0U);
    const virtual_entry *escaped = find(tree, "a%3F");
    REQUIRE(escaped != nullptr);
    CHECK(escaped->source_index == 1U);

    // The manifest itself must not be an ancestor of any other entry.
    const virtual_entry *manifest = manifest_of(tree);
    REQUIRE(manifest != nullptr);
    CHECK(manifest->generated_text == "1\ta?\ta%253F\n");
    for (const virtual_entry &entry : tree.entries) {
        if (&entry == manifest) {
            continue;
        }
        CHECK(!(entry.path.starts_with(manifest->path + "/")));
    }
    CHECK(manifest->path == "name-map~1.tsv");
}

TEST_CASE("godot virtual tree: a generated manifest never renames an unchanged physical source")
{
    const virtual_tree unchanged = build(make_index({"name-map.tsv"}));
    CHECK(manifest_of(unchanged) == nullptr);
    CHECK(unchanged.entries.size() == 1);
    const virtual_tree tree = build(make_index({"name-map.tsv", "a?"}));
    REQUIRE(find(tree, "name-map.tsv") != nullptr);
    CHECK(find(tree, "name-map.tsv")->source_index == 0U);
    const virtual_entry *manifest = manifest_of(tree);
    REQUIRE(manifest != nullptr);
    CHECK(manifest->path == "name-map~1.tsv");
    CHECK(manifest->generated_text == "1\ta?\ta%253F\n");
}

TEST_CASE("godot virtual tree: components above 255 UTF-16 units shorten to a source-index name")
{
    const std::string ascii256(256, 'a');
    const std::string astral = "\xF0\x9F\x98\x80"; // U+1F600: 4 UTF-8 bytes, 2 UTF-16 units
    std::string astral128;
    std::string astral127;
    for (int index = 0; index < 128; ++index) {
        astral128 += astral;
    }
    for (int index = 0; index < 127; ++index) {
        astral127 += astral;
    }
    astral127 += "x"; // 254 + 1 = 255 UTF-16 units: exactly at the limit

    const virtual_tree tree = build(make_index({ascii256, astral128, astral127}));

    const virtual_entry *short_a = find(tree, "0");
    REQUIRE(short_a != nullptr);
    CHECK(short_a->source_index == 0U);
    const virtual_entry *short_b = find(tree, "1");
    REQUIRE(short_b != nullptr);
    CHECK(short_b->source_index == 1U);
    const virtual_entry *boundary = find(tree, "" + astral127);
    REQUIRE(boundary != nullptr);
    CHECK(boundary->source_index == 2U);
    REQUIRE(manifest_of(tree) != nullptr);
}

TEST_CASE("godot virtual tree: a component that triples under escaping shortens to its index")
{
    const std::string invalid(100, static_cast<char>(0xFFU)); // 100 bytes -> 300 escaped chars
    const virtual_tree tree = build(make_index({invalid}));

    const virtual_entry *shortened = find(tree, "0");
    REQUIRE(shortened != nullptr);
    CHECK(shortened->source_index == 0U);
    REQUIRE(manifest_of(tree) != nullptr);
    CHECK(!(manifest_of(tree)->generated_text.empty()));
}

TEST_CASE("godot virtual tree: Unicode superscript device names are escaped case-insensitively")
{
    const std::string superscript_one = "\xC2\xB9";   // U+00B9
    const std::string superscript_two = "\xC2\xB2";   // U+00B2
    const std::string superscript_three = "\xC2\xB3"; // U+00B3
    const virtual_tree tree = build(
        make_index({"COM", "COM0", "COM10", "COM" + superscript_one, "COM" + superscript_two, "COM" + superscript_three,
                    "LPT" + superscript_one, "LPT" + superscript_two, "LPT" + superscript_three,
                    "com" + superscript_one, "lpt" + superscript_three, "COM1", "con"}));

    CHECK(find(tree, "COM") != nullptr);
    CHECK(find(tree, "COM0") != nullptr);
    CHECK(find(tree, "COM10") != nullptr);
    CHECK(find(tree, "%43OM" + superscript_one) != nullptr);
    CHECK(find(tree, "%43OM" + superscript_two) != nullptr);
    CHECK(find(tree, "%43OM" + superscript_three) != nullptr);
    CHECK(find(tree, "%4CPT" + superscript_one) != nullptr);
    CHECK(find(tree, "%4CPT" + superscript_two) != nullptr);
    CHECK(find(tree, "%4CPT" + superscript_three) != nullptr);
    CHECK(find(tree, "%63om" + superscript_one) != nullptr);
    CHECK(find(tree, "%6Cpt" + superscript_three) != nullptr);
    CHECK(find(tree, "%43OM1") != nullptr);
    CHECK(find(tree, "%63on") != nullptr);
}

TEST_CASE("godot virtual tree: the manifest stays valid UTF-8 and keeps original bytes recoverable")
{
    const std::string raw = with_byte("a", 0xFFU) + "b";
    const virtual_tree tree = build(make_index({raw}));

    const virtual_entry *manifest = manifest_of(tree);
    REQUIRE(manifest != nullptr);
    const std::string doc = manifest->generated_text;
    CHECK(valid_utf8(doc));
    REQUIRE(doc.ends_with("\n"));
    const std::vector<std::string> fields = tab_fields(doc.substr(0, doc.size() - 1));
    REQUIRE(fields.size() == 3);
    CHECK(fields[0] == "0");
    CHECK(unescape_field(fields[1]) == raw);
}

TEST_CASE("godot virtual tree: the generated manifest also respects the path limit")
{
    tree_limits limits;
    limits.max_path_chars = 15;

    const virtual_tree tree = build(make_index({"quite/a/fairly/long/path.txt"}), {}, {}, limits);

    const virtual_entry *manifest = manifest_of(tree);
    REQUIRE(manifest != nullptr);
    CHECK(manifest->path.size() <= 15);
    CHECK(manifest->path == "name-map.tsv");
    for (const virtual_entry &entry : tree.entries) {
        CHECK(entry.path.size() <= 15);
    }
    CHECK(find(tree, "0") != nullptr);
}

TEST_CASE("godot virtual tree: every presented path obeys the component limit")
{
    tree_limits limits;
    limits.max_path_chars = 400;

    const std::string long_component(300, 'b');
    const virtual_tree tree = build(make_index({"ok/file.txt", long_component}), {}, {}, limits);

    CHECK(find(tree, "ok/file.txt") != nullptr);
    CHECK(find(tree, "1") != nullptr);
    CHECK(find(tree, "" + long_component) == nullptr);
}

TEST_CASE("godot virtual tree: non-ASCII case variants collide by native ordinal comparison")
{
    const std::string upper = "\xC3\x84.txt"; // Ä.txt
    const std::string lower = "\xC3\xA4.txt"; // ä.txt
    const virtual_tree tree = build(make_index({upper, lower}));

    const virtual_entry *first = find(tree, "" + upper);
    REQUIRE(first != nullptr);
    CHECK(first->source_index == 0U);
    CHECK(find(tree, "" + lower) == nullptr);
    const virtual_entry *renamed = find(tree, "\xC3\xA4~1.txt");
    REQUIRE(renamed != nullptr);
    CHECK(renamed->source_index == 1U);
}

TEST_CASE("godot virtual tree: many fixed-width names keep every index and unique presented path")
{
    std::vector<std::string> names;
    names.reserve(4000);
    for (int index = 0; index < 4000; ++index) {
        names.push_back("f" + std::to_string(100000 + index) + ".bin");
    }
    const virtual_tree tree = build(make_index(names));

    std::set<std::string> raw;
    std::size_t raw_count = 0;
    for (const virtual_entry &entry : tree.entries) {
        if (entry.source_index.has_value() && !entry.converted) {
            ++raw_count;
            raw.insert(entry.path);
        }
    }
    CHECK(raw_count == names.size());
    CHECK(raw.size() == names.size());
}

TEST_CASE("godot virtual tree: manifest suffixes skip physical files and directory ancestors")
{
    const virtual_tree tree = build(make_index({"name-map.tsv/x", "name-map~1.tsv", "a?"}));
    REQUIRE(manifest_of(tree) != nullptr);
    CHECK(manifest_of(tree)->path == "name-map~2.tsv");
    CHECK(find(tree, "name-map.tsv/x") != nullptr);
    CHECK(find(tree, "name-map~1.tsv") != nullptr);
}

TEST_CASE("godot virtual tree: a tiny metadata budget fails before preprocessing")
{
    tree_limits limits;
    limits.max_metadata_bytes = 1;

    CHECK_THROWS_AS(build(make_index({"a.txt"}), {}, {}, limits), tree_error);
    CHECK_THROWS_AS(build(make_index({"a.ctex", "b.ctex"}), {}, {{0, ".png"}, {1, ".png"}}, limits), tree_error);
}

TEST_CASE("godot virtual tree: a budget that passes preflight still fails while charging")
{
    tree_limits limits;
    limits.max_metadata_bytes = 120;

    // Preflight only requires room for the raw entry slots; the charged names exceed it.
    CHECK_THROWS_AS(build(make_index({"a.txt"}), {}, {}, limits), tree_error);

    // The smaller x86 row record needs a tighter limit to fail at its raw-row reservation.
    limits.max_metadata_bytes = 100;
    CHECK_THROWS_AS(build(make_index({"a.txt"}), {}, {}, limits), unpackrat::godot::tree_budget_error);
}

TEST_CASE("godot virtual tree: only a metadata-budget breach uses the typed budget error")
{
    // A metadata-size preflight breach and a charge breach both report the typed budget error, so a
    // caller can retry the tree with fewer optional rows instead of losing the whole listing.
    tree_limits preflight;
    preflight.max_metadata_bytes = 1;
    CHECK_THROWS_AS(build(make_index({"a.txt"}), {}, {}, preflight), unpackrat::godot::tree_budget_error);

    tree_limits charging;
    charging.max_metadata_bytes = 120;
    CHECK_THROWS_AS(build(make_index({"a.txt"}), {}, {}, charging), unpackrat::godot::tree_budget_error);

    // Every structural fault stays the generic tree error and is never a budget error, so a caller
    // propagates it instead of retrying. Zero means no throw, one a budget error, two a plain error.
    const auto category = [](const auto &action) {
        try {
            action();
        } catch (const unpackrat::godot::tree_budget_error &) {
            return 1;
        } catch (const tree_error &) {
            return 2;
        }
        return 0;
    };
    CHECK(category([&] { static_cast<void>(build(make_index({"a.txt"}))); }) == 0);
    CHECK(category([&] { static_cast<void>(build(make_index({"a.txt"}), {}, {{1, ".png"}})); }) == 2);
}

TEST_CASE("godot virtual tree: the aggregate entry budget also covers derived rows")
{
    tree_limits limits;
    limits.max_entries = 2;

    // Two substituted views fit, but their generated path map exceeds the entry budget.
    CHECK_THROWS_AS(build(make_index({"a.ctex", "b.ctex"}), {}, {{0, ".png"}, {1, ".png"}}, limits), tree_error);
}

TEST_CASE("godot virtual tree: mapping and hint counts are part of the entry budget")
{
    tree_limits limits;
    limits.max_entries = 1;

    const pck_index index = make_index({"a.ctex"});
    CHECK_THROWS_AS(build(index, {{"v/a.png", "a.ctex", ""}, {"v/b.png", "a.ctex", ""}}, {}, limits), tree_error);
    CHECK_THROWS_AS(build(make_index({"a.png"}), {}, {{0, ".png"}, {0, ".png"}}, limits), tree_error);
}

TEST_CASE("godot virtual tree: a long logical alias falls back under the default limit")
{
    const std::string long_stem(300, 'q');
    const pck_index index = make_index({".godot/x.ctex"});
    const std::vector<import_mapping> mappings = {{long_stem + ".png", ".godot/x.ctex", ""}};

    const virtual_tree tree = build(index, mappings);

    CHECK(find(tree, long_stem + ".ctex") == nullptr);
    const virtual_entry *fallback = find(tree, "0");
    REQUIRE(fallback != nullptr);
    CHECK(fallback->source_index == 0U);
}

// U+4E2D: three UTF-8 bytes, one UTF-16 code unit. A component of N copies is N UTF-16 code
// units but 3N UTF-8 bytes, which is exactly the byte/UTF-16 confusion under test.
namespace
{
    constexpr std::string_view cjk = "\xE4\xB8\xAD";
}

TEST_CASE("godot virtual tree: multibyte components under the aggregate limit keep their hierarchy")
{
    const std::string component = repeat(cjk, 200); // 200 UTF-16 units, 600 UTF-8 bytes
    const std::string path = component + "/" + component + "/" + component + "/" + component;
    // 4 x 600 bytes + 3 separators = 2403 UTF-8 bytes, but only 803 UTF-16 code units and
    // every component is 200 units, all comfortably inside the default limits.
    REQUIRE(path.size() == 2403);

    const virtual_tree tree = build(make_index({path}));

    // The physical hierarchy is preserved verbatim without a shortening placeholder.
    const virtual_entry *raw = find(tree, "" + path);
    REQUIRE(raw != nullptr);
    CHECK(raw->source_index == 0U);
    CHECK(find(tree, path) != nullptr);
    CHECK(find(tree, "0") == nullptr);
    CHECK(manifest_of(tree) == nullptr);
}

TEST_CASE("godot virtual tree: aggregate and component multibyte limits still shorten near-limit names")
{
    // Five components of 300 CJK characters are 1500 UTF-16 units, past the 1023 aggregate
    // ceiling, so the name shortens to its source index.
    const std::string wide = repeat(cjk, 300);
    std::string aggregate = wide;
    for (int index = 0; index < 4; ++index) {
        aggregate += "/" + wide;
    }
    const virtual_tree too_long = build(make_index({aggregate}));
    CHECK(find(too_long, aggregate) == nullptr);
    REQUIRE(find(too_long, "0") != nullptr);

    // A single 260-unit component breaks the 255-unit per-component ceiling even though the
    // aggregate is far inside 1023.
    const std::string per_component = repeat(cjk, 260);
    const virtual_tree wide_component = build(make_index({per_component}));
    CHECK(find(wide_component, per_component) == nullptr);
    REQUIRE(find(wide_component, "0") != nullptr);

    // A 255-unit component sits exactly on the per-component ceiling and is kept.
    const std::string boundary = repeat(cjk, 255);
    const virtual_tree at_limit = build(make_index({boundary}));
    REQUIRE(find(at_limit, "" + boundary) != nullptr);
    CHECK(find(at_limit, "" + boundary)->source_index == 0U);
}

TEST_CASE("godot virtual tree: a long run of identical names stays bounded and keeps every index")
{
    const std::size_t total = 100000;
    const std::vector<std::string> names(total, "art/dup.png");

    // A generous ceiling isolates the placement cost: every duplicate resolves in constant
    // time regardless of how many identical names precede it.
    tree_limits limits;
    limits.max_metadata_bytes = 128'000'000;
    const virtual_tree tree = build(make_index(names), {}, {}, limits);

    std::set<std::string> raw;
    std::size_t raw_count = 0;
    for (const virtual_entry &entry : tree.entries) {
        if (entry.source_index.has_value() && !entry.converted) {
            ++raw_count;
            raw.insert(entry.path);
        }
    }
    CHECK(raw_count == total);
    CHECK(raw.size() == total);
    CHECK(find(tree, "art/dup.png") != nullptr);
    CHECK(find(tree, "art/dup~1.png") != nullptr);
    CHECK(find(tree, "art/dup~99999.png") != nullptr);
    CHECK(find(tree, "art/dup.png") != nullptr);
    CHECK(find(tree, "art/dup~99999.png") != nullptr);
}

TEST_CASE("godot virtual tree: the raw-row backing store is charged against the metadata budget")
{
    // A large directory must account for its retained raw rows as well as all name indices.
    const std::vector<std::string> names = fixed_width_names(100000);

    tree_limits limits;
    limits.max_metadata_bytes = 25'000'000;
    CHECK_THROWS_AS(build(make_index(names), {}, {}, limits), tree_error);

    // The same shape still builds once the budget covers the reservation.
    tree_limits ample;
    ample.max_metadata_bytes = 120'000'000;
    const virtual_tree tree = build(make_index(names), {}, {}, ample);
    CHECK(find(tree, "f000000") != nullptr);
    CHECK(find(tree, "f099999") != nullptr);
}

TEST_CASE("godot virtual tree: an oversized mapping path fails the budget before it is retained")
{
    const pck_index index = make_index({".godot/x.ctex"});
    const std::string huge(5000, 'q');
    const std::vector<import_mapping> mappings = {{huge + ".png", ".godot/x.ctex", ""}};

    tree_limits limits;
    limits.max_metadata_bytes = 1000;
    CHECK_THROWS_AS(build(index, mappings, {}, limits), tree_error);

    // With a real budget the mapping still builds and the over-long alias falls back.
    const virtual_tree tree = build(index, mappings);
    REQUIRE(find(tree, "0") != nullptr);
    CHECK(find(tree, "0")->source_index == 0U);
}

TEST_CASE("godot virtual tree: escaped manifest metadata is accounted against the total budget")
{
    // One entry whose high bytes are not valid UTF-8 and whose UTF-16 length is past the
    // aggregate ceiling. The presented raw path shortens to its source index, but the generated
    // name-map row still records the original name, so escaping that single field costs about
    // three bytes per input byte. That escaped row, together with the row text retained on the
    // generated manifest entry, dominates the retained metadata.
    const std::string raw = "a/" + std::string(1030, '\xFF');

    const virtual_tree ample = build(make_index({raw}));
    const virtual_entry *ample_manifest = manifest_of(ample);
    REQUIRE(ample_manifest != nullptr);
    CHECK(valid_utf8(ample_manifest->generated_text));
    CHECK(ample_manifest->generated_text.ends_with("\n"));

    // The row is the escaped original name followed by its placed raw path.
    const std::vector<std::string> fields = tab_fields(ample_manifest->generated_text);
    REQUIRE(fields.size() == 3);
    CHECK(fields[0] == "0");
    CHECK(unescape_field(fields[1]) == raw);

    // For a single escaped row the generated text is the exact charged row bound, so the boundary
    // is read from real build output instead of a host struct-size estimate that would only hold
    // on one architecture.
    const std::size_t row_bound = ample_manifest->generated_text.size();
    CHECK(row_bound > raw.size() * 2);

    // The public entry point reports every breach with one generic ``tree_error`` and never names
    // an internal structure or a charge order, so these two budgets bracket the total escaped-
    // manifest accounting boundary instead of pinning a particular allocation order.

    // A budget that only covers the escaped row itself cannot also hold the tree's base metadata
    // and the retained manifest text, so the total accounting rejects the whole tree.
    tree_limits tight;
    tight.max_metadata_bytes = row_bound;
    CHECK_THROWS_AS(build(make_index({raw}), {}, {}, tight), tree_error);

    // A budget a few times the escaped-row cost, still read from the successful build above, holds
    // the base metadata, the escaped row and the retained manifest text at once. The build then
    // succeeds and reproduces the manifest byte for byte, so the tight failure above is a total-
    // budget boundary and not the inability to represent a single row.
    tree_limits sufficient;
    sufficient.max_metadata_bytes = row_bound * 3;
    const virtual_tree rebuilt = build(make_index({raw}), {}, {}, sufficient);
    CHECK(rebuilt.entries.size() == ample.entries.size());
    const virtual_entry *rebuilt_manifest = manifest_of(rebuilt);
    REQUIRE(rebuilt_manifest != nullptr);
    CHECK(rebuilt_manifest->generated_text == ample_manifest->generated_text);
}

TEST_CASE("godot virtual tree: audio conversion substitutes the truthful original")
{
    const virtual_tree tree = build(make_index({".godot/imported/theme-h.sample"}),
                                    {{"audio/theme.wav", ".godot/imported/theme-h.sample", ""}}, {{0, ".wav"}});
    CHECK(find(tree, "audio/theme.sample") == nullptr);
    REQUIRE(find(tree, "audio/theme.wav") != nullptr);
    CHECK(find(tree, "audio/theme.wav")->converted);
    REQUIRE(manifest_of(tree) != nullptr);
    CHECK(manifest_of(tree)->generated_text == "0\t.godot/imported/theme-h.sample\taudio/theme.wav\n");
}
TEST_CASE("godot virtual tree: a substituted view keeps a short fallback and visible path mapping")
{
    tree_limits limits;
    limits.max_path_chars = 2;
    const virtual_tree tree = build(make_index({"long.ctex"}), {}, {{0, ".png"}}, limits);
    CHECK(find(tree, "0") == nullptr);
    const virtual_entry *converted = find(tree, "1");
    REQUIRE(converted != nullptr);
    CHECK(converted->converted);
    REQUIRE(manifest_of(tree) != nullptr);
    CHECK(manifest_of(tree)->path == "n");
}

TEST_CASE("godot virtual tree: decoded mode substitutes supported resources and keeps raw fallbacks",
          "[godot][decode-settings]")
{
    const auto index = make_index({".godot/pixel.ctex", ".godot/unknown.ctex", "art/pixel.png.import", "readme.txt"});
    const std::vector<import_mapping> mappings{{"art/pixel.png", ".godot/pixel.ctex", ""},
                                               {"art/unknown.png", ".godot/unknown.ctex", ""}};
    const auto tree = build(index, mappings, {{0, ".png"}});
    REQUIRE(find(tree, "art/pixel.png") != nullptr);
    CHECK(find(tree, "art/pixel.png")->converted);
    CHECK(find(tree, "art/pixel.ctex") == nullptr);
    REQUIRE(find(tree, "art/unknown.ctex") != nullptr);
    CHECK(!find(tree, "art/unknown.ctex")->converted);
    CHECK(find(tree, "art/pixel.png.import") != nullptr);
    CHECK(find(tree, "readme.txt") != nullptr);
    REQUIRE(manifest_of(tree) != nullptr);
    CHECK(manifest_of(tree)->generated_text.find("0\t.godot/pixel.ctex\tart/pixel.png\n") != std::string::npos);
}

TEST_CASE("godot virtual tree: original mode uses truthful extensions without converted duplicates",
          "[godot][decode-settings]")
{
    const auto index = make_index({".godot/pixel.ctex", "art/pixel.png.import"});
    const auto tree = build(index, {{"art/pixel.png", ".godot/pixel.ctex", ""}});
    REQUIRE(find(tree, "art/pixel.ctex") != nullptr);
    CHECK(!find(tree, "art/pixel.ctex")->converted);
    CHECK(find(tree, "art/pixel.png") == nullptr);
    CHECK(find(tree, "art/pixel.png.import") != nullptr);
}

TEST_CASE("godot virtual tree: patch removals are metadata instead of extractable empty files",
          "[godot][decode-settings]")
{
    auto index = make_index({"old/removed.txt", "live.txt", "patch-removals.tsv"});
    index.entries[0].kind = unpackrat::godot::pck_entry_kind::removal;
    const auto tree = build(index);
    CHECK(find(tree, "old/removed.txt") == nullptr);
    REQUIRE(find(tree, "live.txt") != nullptr);
    REQUIRE(find(tree, "patch-removals.tsv") != nullptr);
    const auto *removals = find(tree, "patch-removals~1.tsv");
    REQUIRE(removals != nullptr);
    CHECK(!removals->source_index.has_value());
    CHECK(removals->generated_text == "0\told/removed.txt\n");
    CHECK(!removals->converted);
}

TEST_CASE("godot virtual tree: removal records cannot be converted through a hint", "[godot][decode-settings]")
{
    auto index = make_index({"old/removed.ctex"});
    index.entries[0].kind = unpackrat::godot::pck_entry_kind::removal;
    CHECK_THROWS_AS(build(index, {}, {{0, ".png"}}), tree_error);
}
