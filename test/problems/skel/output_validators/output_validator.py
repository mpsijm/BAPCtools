#!/usr/bin/env python3
import math
import sys

try:
    # Do not use the default_output_validator.cpp, because it's wasteful to compile it for every test
    if math.isclose(
        4 * float(open(sys.argv[1]).read()) ** 0.5,
        float(open(sys.argv[2]).read()),
        rel_tol=1e-6,
        abs_tol=1e-6,
    ):
        exit(42)
    else:
        print("WA")
        exit(43)
except Exception as e:
    print(e)
    exit(43)
