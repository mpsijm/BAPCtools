#!/usr/bin/env python3
import random
import sys

random.seed(sys.argv[1])
print(random.randrange(0, 1000000))
