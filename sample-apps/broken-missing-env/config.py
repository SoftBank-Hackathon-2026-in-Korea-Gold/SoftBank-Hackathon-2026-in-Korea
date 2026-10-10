import os


def development() -> dict:
    return {"DEBUG": True, "DB_HOST": os.environ.get("DB_HOST", "localhost")}


def production() -> dict:
    # required in production: injected by the platform, never baked into the image
    return {
        "DEBUG": False,
        "SECRET_KEY": os.environ["SECRET_KEY"],
        "DB_HOST": os.environ["DB_HOST"],
    }


PROFILES = {"development": development, "production": production}


def load() -> dict:
    return PROFILES[os.environ.get("APP_ENV", "production")]()
