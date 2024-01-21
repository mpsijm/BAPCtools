#!/usr/bin/env python3

try:
    if 0 <= int(input()) < 1_000_000:
        exit(42)
    else:
        print("Out of range")
        exit(43)
except Exception as e:
    print(e)
    exit(43)
