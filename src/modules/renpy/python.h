#ifndef PYTHON_H
#define PYTHON_H

#include <memory>
#include <string>

namespace renpy
{
    class python_object
    {
    public:
        virtual ~python_object() = default;
    };

    class python
    {
    public:
        python() noexcept;

        python(const python &) = delete;

        python(python &&) = delete;

        python &operator=(const python &) = delete;

        python &operator=(python &&) = delete;

        std::unique_ptr<python_object> unpickle(const std::string &pickled_string);

        ~python();
    };
}

#endif
