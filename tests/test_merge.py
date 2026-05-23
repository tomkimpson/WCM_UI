from worker.merge import merge_params


def test_user_overrides_default():
    defaults = {"simulation": {"length_sec": 60, "seed": 0}}
    user = {"simulation": {"length_sec": 120}}
    assert merge_params(defaults, user) == {
        "simulation": {"length_sec": 120, "seed": 0},
    }


def test_user_can_be_empty():
    defaults = {"simulation": {"length_sec": 60}}
    assert merge_params(defaults, {}) == defaults
    assert merge_params(defaults, None) == defaults


def test_merge_is_recursive():
    defaults = {"a": {"x": 1, "y": 2}, "b": 3}
    user = {"a": {"y": 20}}
    assert merge_params(defaults, user) == {"a": {"x": 1, "y": 20}, "b": 3}


def test_merge_does_not_mutate_inputs():
    defaults = {"simulation": {"length_sec": 60}}
    user = {"simulation": {"length_sec": 120}}
    _ = merge_params(defaults, user)
    assert defaults == {"simulation": {"length_sec": 60}}
    assert user == {"simulation": {"length_sec": 120}}


def test_user_value_overrides_even_if_falsy():
    """seed=0 is a legitimate value; merge must not treat it as 'missing'."""
    defaults = {"simulation": {"seed": 42}}
    user = {"simulation": {"seed": 0}}
    assert merge_params(defaults, user)["simulation"]["seed"] == 0
