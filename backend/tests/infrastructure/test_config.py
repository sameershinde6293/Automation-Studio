from app.infrastructure.config.settings import Settings

def test_settings_defaults():
    settings = Settings()
    assert settings.APP_NAME == "Creator OS Backend"
    assert settings.VERSION == "0.1.0"
