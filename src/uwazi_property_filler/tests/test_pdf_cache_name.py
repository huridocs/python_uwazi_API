from uwazi_property_filler.adapters.pdf_cache_store import safe_name


def test_safe_name_keeps_valid_storage_name() -> None:
    assert safe_name("1700000000123abc.pdf") == "1700000000123abc.pdf"


def test_safe_name_replaces_path_separators() -> None:
    assert safe_name("a/b\\c.pdf") == "a_b_c.pdf"


def test_safe_name_replaces_other_unsafe_chars() -> None:
    assert safe_name("a b:c.pdf") == "a_b_c.pdf"
