from pydantic_settings import BaseSettings, SettingsConfigDict

from app.version import __version__

class Settings(BaseSettings):
    APP_NAME: str = "Creator OS Backend"
    VERSION: str = __version__
    ENVIRONMENT: str = "development"
    DATABASE_URL: str = "sqlite:///./creator_os.db"
    
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

settings = Settings()
