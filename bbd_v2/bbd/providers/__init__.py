from .booru import GelbooruProvider, KonachanProvider, YandereProvider
from .danbooru import DanbooruProvider
from .sankaku import SankakuProvider
from .pixiv import PixivProvider
from .twitter import TwitterProvider

PROVIDER_CLASSES = [
    DanbooruProvider,
    GelbooruProvider,
    KonachanProvider,
    YandereProvider,
    SankakuProvider,
    PixivProvider,
    TwitterProvider,
]

def make_providers(config):
    return [cls(config) for cls in PROVIDER_CLASSES]
