"""
nanobot - A lightweight AI agent framework
"""

import warnings

# Suppress harmless version-mismatch warning from the `requests` library
# (triggered by transitive dependency version skew with urllib3/chardet).
warnings.filterwarnings("ignore", message="urllib3.*doesn't match a supported version")

__version__ = "0.1.4.post3"
__logo__ = "🐈"
