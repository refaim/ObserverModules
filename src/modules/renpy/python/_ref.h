#pragma once

#include "python.h"

#include <Python.h>
#include <stdexcept>

namespace python
{
    class ref : public object
    {
    public:
        explicit ref(PyObject *object)
        {
            if (object == nullptr) {
                throw std::invalid_argument("object is null");
            }
            object_ = object;
        }

        ~ref() override = default;

        [[nodiscard]] PyObject *get() const noexcept
        {
            return object_;
        }

    protected:
        PyObject *object_;
    };

    class weak_ref final : public ref
    {
    public:
        weak_ref() = delete;

        weak_ref(const weak_ref &) = delete;

        weak_ref(weak_ref &&) = delete;

        weak_ref &operator=(const weak_ref &) = delete;

        weak_ref &operator=(weak_ref &&) = delete;

        explicit weak_ref(PyObject *object) : ref(object)
        {
        }

        ~weak_ref() override
        {
            object_ = nullptr;
        }
    };

    class strong_ref final : public ref
    {
    public:
        strong_ref() = delete;

        strong_ref(const strong_ref &) = delete;

        strong_ref(strong_ref &&) = delete;

        strong_ref &operator=(const strong_ref &) = delete;

        strong_ref &operator=(strong_ref &&) = delete;

        explicit strong_ref(PyObject *object) : ref(object)
        {
        }

        ~strong_ref() override
        {
            Py_DECREF(object_);
            object_ = nullptr;
        }
    };

    inline PyObject *rawptr(const object &object)
    {
        return dynamic_cast<const ref &>(object).get();
    }
}
