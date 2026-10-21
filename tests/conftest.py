import pytest

from fin import db


@pytest.fixture
def con():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def account(con):
    return con.execute(
        "INSERT INTO accounts (key, name, institution) VALUES ('iban:PT50000000000000000000001', 'Main', 'Millennium BCP')"
    ).lastrowid
