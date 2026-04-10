# qa/adapters — Platform adapters for web and mobile testing
from .protocol import PlatformAdapter
from .mobile import MobileAdapter
from .web import WebAdapter
from qa.models.common import Platform


def make_adapter(platform: Platform | str, **kwargs) -> PlatformAdapter:
    """Factory: create the right adapter based on platform.

    Usage:
        adapter = make_adapter("mobile", device_id="RZCXA21GV9P")
        adapter = make_adapter(Platform.WEB)
    """
    if isinstance(platform, str):
        platform = Platform(platform)

    match platform:
        case Platform.WEB:
            return WebAdapter()
        case Platform.MOBILE:
            device_id = kwargs.get("device_id", "")
            return MobileAdapter(device_id=device_id)
        case _:
            raise ValueError(f"Unknown platform: {platform}")
