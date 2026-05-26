#!/usr/bin/env python3
"""
FinSight Studio - Dashboard Generator Runner
Thin wrapper that redirects execution to the modular src/dashboard.py script.
"""

import os
import sys

# Add the root directory to path so all internal src imports resolve properly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.dashboard import main

if __name__ == "__main__":
    main()
