#ifndef PYTHON_H
#define PYTHON_H

#include <generator>
#include <memory>
#include <string>
#include <vector>

namespace python
{
    class object
    {
    public:
        virtual ~object() = default;
    };

    class context final
    {
    public:
        context() noexcept;

        context(const context &) = delete;

        context(context &&) = delete;

        context &operator=(const context &) = delete;

        context &operator=(context &&) = delete;

        [[nodiscard]] std::unique_ptr<object> unpickle(const std::string &pickled_string) const;

        [[nodiscard]] int64_t as_int64(const object &value) const;

        [[nodiscard]] std::string as_bytes(const object &value) const;

        ~context();
    };

    class container
    {
    public:
        explicit container(const context &context, std::unique_ptr<object> object) : object_(std::move(object))
        {
        }

        [[nodiscard]] virtual int64_t size() const = 0;

        virtual ~container() = default;

    protected:
        std::unique_ptr<object> object_;
    };

    class tuple final : public container
    {
    public:
        explicit tuple(const context &context, std::unique_ptr<object> object) : container(context, std::move(object))
        {
        }

        [[nodiscard]] int64_t size() const override;

        [[nodiscard]] std::unique_ptr<object> get_item(int64_t index) const;
    };

    class list final : public container
    {
    public:
        explicit list(const context &context, std::unique_ptr<object> object) : container(context, std::move(object))
        {
        }

        [[nodiscard]] int64_t size() const override;

        [[nodiscard]] std::unique_ptr<object> get_item(int64_t index) const;
    };

    class dict final : public container
    {
    public:
        explicit dict(const context &context, std::unique_ptr<object> object) : container(context, std::move(object))
        {
        }

        [[nodiscard]] int64_t size() const override;

        [[nodiscard]] std::generator<std::pair<std::string_view, std::unique_ptr<object> > > iterate() const;
    };
}

#endif
