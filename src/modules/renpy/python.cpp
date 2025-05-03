#include "python.h"

#include <Python.h>
#include <stdexcept>

namespace renpy
{
    class weak_ref final : public python_object
    {
    public:
        weak_ref() = delete;

        explicit weak_ref(PyObject *object)
        {
            if (object == nullptr) {
                throw std::invalid_argument("object is null");
            }
            object_ = object;
        }

        weak_ref(const weak_ref &) = delete;

        weak_ref(weak_ref &&) = delete;

        weak_ref &operator=(const weak_ref &) = delete;

        weak_ref &operator=(weak_ref &&) = delete;

        ~weak_ref() override
        {
            object_ = nullptr;
        }

        PyObject *get_object() const noexcept
        {
            return object_;
        }

    private:
        PyObject *object_;
    };

    class strong_ref final : public python_object
    {
    public:
        strong_ref() = delete;

        explicit strong_ref(PyObject *object)
        {
            if (object == nullptr) {
                throw std::invalid_argument("object is null");
            }
            object_ = object;
        }

        strong_ref(const strong_ref &) = delete;

        strong_ref(strong_ref &&) = delete;

        strong_ref &operator=(const strong_ref &) = delete;

        strong_ref &operator=(strong_ref &&) = delete;

        ~strong_ref() override
        {
            Py_DECREF(object_);
            object_ = nullptr;
        }

        PyObject *get_object() const noexcept
        {
            return object_;
        }

    private:
        PyObject *object_;
    };

    python::python() noexcept
    {
        Py_Initialize();
    }

    python::~python()
    {
        Py_Finalize();
    }

    // ReSharper disable once CppMemberFunctionMayBeStatic
    std::unique_ptr<python_object> python::unpickle(const std::string &pickled_string)
    {
        const auto pickled_bytes = weak_ref(
            PyByteArray_FromStringAndSize(pickled_string.c_str(), pickled_string.size()));

        const auto args = weak_ref(PyTuple_New(1));
        if (PyTuple_SetItem(args.get_object(), 0, pickled_bytes.get_object()) != 0) {
            throw std::logic_error("Unable to compose pickle.loads() arguments");
        }

        const auto kwargs = strong_ref(PyDict_New());
        if (PyDict_SetItemString(kwargs.get_object(), "encoding",
                                 weak_ref(PyUnicode_FromString("latin1")).get_object()) != 0) {
            throw std::logic_error("Unable to compose pickle.loads() keyword arguments");
        }

        const auto pickle = strong_ref(PyImport_Import(strong_ref(PyUnicode_FromString("pickle")).get_object()));
        const auto loads = strong_ref(PyObject_GetAttrString(pickle.get_object(), "loads"));
        return std::make_unique<strong_ref>(PyObject_Call(loads.get_object(), args.get_object(), kwargs.get_object()));
    }
}
