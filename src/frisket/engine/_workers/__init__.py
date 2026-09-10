"""Private subprocess worker implementations.

Modules in this package must stay cheap to import in the parent process.  Heavy
runtime imports (ONNX Runtime, NumPy, PyAV, model libraries) belong inside a
worker entry point after the sandbox owns the child process.
"""
