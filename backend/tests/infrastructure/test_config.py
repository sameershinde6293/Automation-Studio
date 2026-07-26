import re

from app.infrastructure.config.settings import Settings, settings
from app.version import __version__


def test_settings_defaults():
    s = Settings()
    assert s.APP_NAME == "Creator OS Backend"
    # Version is sourced from app.version -- never hardcode it twice.
    assert s.VERSION == __version__


def test_version_is_semver():
    assert re.match(r"^\d+\.\d+\.\d+", __version__), __version__


def test_module_level_settings_singleton():
    assert settings.APP_NAME == "Creator OS Backend"
    assert settings.VERSION == __version__
