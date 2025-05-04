#include "python.h"
#include "_ref.h"

#include <stdexcept>

#include <Python.h>

namespace python
{
    int64_t dict::size() const
    {
        return PyDict_Size(rawptr(*object_));
    }

    std::generator<std::pair<std::string_view, std::unique_ptr<object> > > dict::iterate() const
    {
        Py_ssize_t pos = 0;
        PyObject *key_object = nullptr, *value_object = nullptr;
        while (PyDict_Next(rawptr(*object_), &pos, &key_object, &value_object)) {
            Py_ssize_t key_len = 0;
            const auto key_bytes = PyUnicode_AsUTF8AndSize(key_object, &key_len);
            if (key_bytes == nullptr || key_len <= 0) {
                throw std::runtime_error("Unable to convert dict key to UTF-8");
            }
            co_yield {std::string_view(key_bytes, key_len), std::make_unique<weak_ref>(value_object)};
        }
    }
}
