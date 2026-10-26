import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_url_handles_special_characters_and_redacts_password() -> None:
    password = "p@ss:/?#%word"
    settings = Settings(_env_file=None, database_password=password)
    assert settings.database_url.password == password
    assert password not in str(settings.database_url)
    assert password not in repr(settings)


@pytest.mark.parametrize("port", [0, 65536])
def test_reject_invalid_database_port(port: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_password="test", database_port=port)
