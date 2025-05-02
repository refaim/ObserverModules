#include <memory>

#include "python.h"

namespace renpy::python
{
    Context::Context() noexcept
    {
        Py_Initialize();
    }

    Context::~Context()
    {
        Py_Finalize();
    }

    WeakReference::WeakReference(PyObject *ptr)
    {
        if (ptr == nullptr)
            throw NullReferenceError();
        raw_ = ptr;
    }

    WeakReference::~WeakReference()
    {
        raw_ = nullptr;
    }

    PyObject *WeakReference::Raw() noexcept
    {
        return raw_;
    }

    StrongReference::StrongReference(PyObject *ptr) : WeakReference(ptr)
    {
    }

    StrongReference::~StrongReference()
    {
        Py_DECREF(raw_);
    }

    std::unique_ptr<WeakReference> MakeWeakRef(PyObject *ptr)
    {
        return std::make_unique<WeakReference>(ptr);
    }

    std::unique_ptr<StrongReference> MakeStrongRef(PyObject *ptr)
    {
        return std::make_unique<StrongReference>(ptr);
    }

    PyObject *RawPtr(std::unique_ptr<WeakReference> &ref) noexcept
    {
        return ref.get()->Raw();
    }

    PyObject *RawPtr(std::unique_ptr<StrongReference> &ref) noexcept
    {
        return ref.get()->Raw();
    }
}
