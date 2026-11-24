from configlib import Config, with_env


def test_other_variables_are_ignored() -> None:
    config: Config = {"server": {"port": 8080}}
    assert with_env(config, {"HOME": "/root", "PATH": "/bin"}) == config


def test_the_input_is_not_changed() -> None:
    config: Config = {"server": {"port": 8080}}
    with_env(config, {"APP_SERVER__PORT": "9090"})
    assert config == {"server": {"port": 8080}}
