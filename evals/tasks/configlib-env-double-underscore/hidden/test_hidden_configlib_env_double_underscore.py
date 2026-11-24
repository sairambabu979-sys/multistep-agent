from configlib import with_env


def test_section_and_key_are_split_at_the_double_underscore():
    env = {"APP_DB__PORT": "5433", "APP_DB__MAX_CONNECTIONS": "20", "APP_SERVER__DEBUG": "true"}
    assert with_env({"db": {"port": 5432, "name": "main"}}, env) == {
        "db": {"port": 5433, "name": "main", "max_connections": 20},
        "server": {"debug": True},
    }


def test_no_double_underscore_is_not_an_override():
    assert with_env({}, {"APP_DEBUG": "1", "APP_DB_PORT": "1"}) == {}


def test_names_are_case_insensitive():
    assert with_env({}, {"APP_Db__Port": "1"}) == {"db": {"port": 1}}


def test_an_empty_section_or_key_is_ignored():
    assert with_env({}, {"APP___PORT": "1", "APP_DB__": "1"}) == {}
