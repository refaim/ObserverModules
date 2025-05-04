#include "python.h"
#include "_ref.h"

#include <Python.h>

namespace python
{
    int64_t list::size() const
    {
        return PyList_Size(rawptr(*object_));
    }

    std::unique_ptr<object> list::get_item(const int64_t index) const
    {
        if (index < 0 || index >= size()) {
            throw std::out_of_range("index out of range");
        }
        return std::make_unique<weak_ref>(PyList_GetItem(rawptr(*object_), index));
    }
}
