#ifndef RENPY_PYTHON_H_
#define RENPY_PYTHON_H_

#include <exception>
#include <memory>

#include <python3.7/Python.h>

namespace renpy::python
{
    class NullReferenceError : public std::exception {};

    class Context
    {
    public:
        Context() noexcept;
        Context(const Context&) = delete;
        Context(Context&&) = delete;
        Context& operator=(const Context&) = delete;
        Context& operator=(Context&&) = delete;
        ~Context();
    };

    class WeakReference
    {
    public:
        WeakReference() = delete;
        WeakReference(PyObject* ptr);
        WeakReference(const WeakReference&) = delete;
        WeakReference(WeakReference&&) = delete;
        WeakReference& operator=(const WeakReference&) = delete;
        WeakReference& operator=(WeakReference&&) = delete;
        virtual ~WeakReference();
        virtual PyObject* Raw() noexcept;
    protected:
        PyObject* raw_;
    };

    class StrongReference : public WeakReference
    {
    public:
        StrongReference() = delete;
        StrongReference(PyObject* ptr);
        StrongReference(const StrongReference&) = delete;
        StrongReference(StrongReference&&) = delete;
        StrongReference& operator=(const StrongReference&) = delete;
        StrongReference& operator=(StrongReference&&) = delete;
        ~StrongReference();
    };

    std::unique_ptr<WeakReference> MakeWeakRef(PyObject* ptr);
    std::unique_ptr<StrongReference> MakeStrongRef(PyObject* ptr);
    PyObject* RawPtr(std::unique_ptr<WeakReference>& ref) noexcept;
    PyObject* RawPtr(std::unique_ptr<StrongReference>& ref) noexcept;
}

#endif // RENPY_PYTHON_H_
