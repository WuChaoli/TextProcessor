from datajuicer_service.profiles.io import InputLimits
from datajuicer_service.profiles.text_exact_minhash_v1 import (
    PROFILE_NAME,
    TextExactMinhashV1,
)


class UnknownProfileError(LookupError):
    pass


def get_profile(name: str, limits: InputLimits) -> TextExactMinhashV1:
    if name != PROFILE_NAME:
        raise UnknownProfileError("UNKNOWN_PROFILE")
    return TextExactMinhashV1(limits)
