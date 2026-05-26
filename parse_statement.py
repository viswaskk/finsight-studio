#!/usr/bin/env python3
"""
FinSight Studio - Statement Parser Engine Runner
Thin wrapper that redirects execution to the modular src/parser.py script.
"""

import os
import sys

# Add the root directory to path so all internal src imports resolve properly
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.parser import parse_pdf, sort_files_by_date, is_bill_payment, load_card_mappings, main

if __name__ == "__main__":
    main()
