from uwazi_property_filler.configuration import instance_key


def test_trailing_slash_is_normalized() -> None:
    assert instance_key("https://x.io/") == instance_key("https://x.io")


def test_differs_across_hosts() -> None:
    assert instance_key("https://a.io") != instance_key("https://b.io")


def test_deterministic() -> None:
    assert instance_key("https://x.io") == instance_key("https://x.io")
