"""Public C/C++ metadata query API.

The implementation still lives in ``src.linux_meta`` for compatibility with
older imports, but new code should prefer this package or ``src.cpp_meta_query``.
"""

from src.linux_meta import (
    analyze_function,
    export_source_bundle,
    export_subfunction_source_bundle,
    print_function_call_sequence,
    print_function_param_constraints,
)

__all__ = [
    "analyze_function",
    "export_source_bundle",
    "export_subfunction_source_bundle",
    "print_function_call_sequence",
    "print_function_param_constraints",
]
