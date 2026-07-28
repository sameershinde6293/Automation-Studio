"""Single source of truth for the Creator OS backend version.

Keeping the version in one module prevents the drift that caused the V1.0
``test_settings_defaults`` failure (settings said ``1.0.1-alpha`` while the test
asserted ``0.1.0``).
"""

__version__ = "1.1.1"

VERSION = __version__
