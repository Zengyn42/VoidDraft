from dataclasses import dataclass, field
from abc import ABC, abstractmethod
from typing import Iterator


@dataclass
class Post:
    id: str
    title: str
    url: str            # canonical post URL
    text: str           # body/description text
    source: str         # "reddit" / "rednote"
    image_urls: list[str] = field(default_factory=list)   # direct image CDN URLs
    video_urls: list[str] = field(default_factory=list)   # direct video CDN URLs
    extra: dict = field(default_factory=dict)             # source-specific metadata


class PlatformSource(ABC):
    @abstractmethod
    def get_posts(self, max_posts: int) -> Iterator[Post]: ...
