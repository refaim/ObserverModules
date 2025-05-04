#include "python.h"
#include "_ref.h"

#include <algorithm>
#include <iterator>
#include <stdexcept>

#include <Python.h>

namespace python
{
    context::context() noexcept
    {
        Py_Initialize();
    }

    context::~context()
    {
        Py_Finalize();
    }

    // ReSharper disable once CppMemberFunctionMayBeStatic
    std::unique_ptr<object> context::unpickle(const std::string &pickled_string) const // NOLINT(*-convert-member-functions-to-static)
    {
        // Using weak_ref because PyTuple_SetItem "steals" a reference
        const auto pickled_bytes = weak_ref(
            PyByteArray_FromStringAndSize(pickled_string.c_str(), std::ssize(pickled_string)));

        const auto args = strong_ref(PyTuple_New(1));
        if (PyTuple_SetItem(args.get(), 0, pickled_bytes.get()) != 0) {
            throw std::logic_error("Unable to compose pickle.loads() arguments");
        }

        const auto kwargs = strong_ref(PyDict_New());
        if (PyDict_SetItemString(kwargs.get(), "encoding",
                                 strong_ref(PyUnicode_FromString("latin1")).get()) != 0) {
            throw std::logic_error("Unable to compose pickle.loads() keyword arguments");
        }

        const auto pickle = strong_ref(PyImport_Import(strong_ref(PyUnicode_FromString("pickle")).get()));
        const auto loads = strong_ref(PyObject_GetAttrString(pickle.get(), "loads"));
        return std::make_unique<strong_ref>(PyObject_Call(loads.get(), args.get(), kwargs.get()));
    }

    // ReSharper disable once CppMemberFunctionMayBeStatic
    int64_t context::as_int64(const object &value) const // NOLINT(*-convert-member-functions-to-static)
    {
        const auto result = PyLong_AsLongLong(rawptr(value));
        if (result == -1 && PyErr_Occurred() != nullptr) {
            throw std::runtime_error("Unable to convert Python object to int64");
        }
        return result;
    }

    // ReSharper disable once CppMemberFunctionMayBeStatic
    std::string context::as_bytes(const object &unicode) const // NOLINT(*-convert-member-functions-to-static)
    {
        const auto ansi = std::make_unique<strong_ref>(PyUnicode_AsLatin1String(rawptr(unicode)));
        auto result = std::string();
        if (const auto size = PyBytes_Size(rawptr(*ansi)); size > 0) {
            const auto bytes = PyBytes_AsString(rawptr(*ansi));
            if (bytes == nullptr) {
                throw std::runtime_error("Unable to convert Python latin1 string to bytes");
            }
            result.assign(bytes, size);
        }
        return result;
    }
}
