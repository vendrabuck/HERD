from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    secret_key: str
    algorithm: str = "HS256"
    cors_origins: str = ""

    model_config = {"env_file": ".env", "case_sensitive": False}


settings = Settings()
