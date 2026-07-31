from datajuicer_service.profiles import exact
from datajuicer_service.profiles.exact import group_exact
from datajuicer_service.profiles.models import ExactGroup, InputSample


def test_exact_grouping_ignores_outer_whitespace_only() -> None:
    samples = [
        InputSample(uid=0, text="  A 1!\n"),
        InputSample(uid=1, text="A 1!"),
        InputSample(uid=2, text="a 1!"),
        InputSample(uid=3, text="A1!"),
    ]

    result = group_exact(samples)

    assert result.groups == (ExactGroup(member_uids=(0, 1), representative_uid=0),)
    assert result.independent_uids == (2, 3)


def test_exact_grouping_is_deterministic_for_unsorted_input() -> None:
    samples = [
        InputSample(uid=8, text="same"),
        InputSample(uid=5, text="unique"),
        InputSample(uid=3, text=" same "),
        InputSample(uid=1, text="other"),
        InputSample(uid=2, text="other"),
    ]

    result = group_exact(samples)

    assert result.groups == (
        ExactGroup(member_uids=(1, 2), representative_uid=1),
        ExactGroup(member_uids=(3, 8), representative_uid=3),
    )
    assert result.independent_uids == (5,)


def test_exact_grouping_compares_text_inside_digest_bucket(monkeypatch) -> None:
    monkeypatch.setattr(exact, "_digest_text", lambda _text: b"same-digest")
    samples = [
        InputSample(uid=0, text="alpha"),
        InputSample(uid=1, text="beta"),
        InputSample(uid=2, text=" alpha "),
    ]

    result = group_exact(samples)

    assert result.groups == (ExactGroup(member_uids=(0, 2), representative_uid=0),)
    assert result.independent_uids == (1,)
