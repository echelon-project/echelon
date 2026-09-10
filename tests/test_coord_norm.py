"""Tests for echelon_engine.atoms.coord_norm — the pure coordinate normaliser leaf."""
from echelon_engine.atoms.coord_norm import norm_coord, _norm_coord


def test_lowercase():
    assert norm_coord("Tooling:CLI") == "tooling:cli"


def test_non_alnum_collapsed_to_underscore():
    assert norm_coord("tooling:my-flag") == "tooling:my_flag"


def test_leading_trailing_underscores_stripped_per_segment():
    assert norm_coord("tooling: _cli_ ") == "tooling:cli"


def test_empty_segments_dropped():
    assert norm_coord("tooling::cli") == "tooling:cli"


def test_empty_string_returns_empty():
    assert norm_coord("") == ""


def test_none_safe():
    # _norm_coord is the internal alias; both should behave identically
    assert _norm_coord(None) == ""  # type: ignore[arg-type]


def test_multi_level_coordinate():
    assert norm_coord("action:read_file:path memory") == "action:read_file:path_memory"


def test_already_normalised_is_idempotent():
    coord = "tooling:cli:flag"
    assert norm_coord(coord) == coord


def test_alias_equals_main():
    assert norm_coord("Foo:Bar-Baz") == _norm_coord("Foo:Bar-Baz")
