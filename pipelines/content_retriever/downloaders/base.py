from abc import ABC, abstractmethod
from pathlib import Path


class FileDownloader(ABC):
    @abstractmethod
    def can_handle(self, url: str) -> bool: ...

    @abstractmethod
    def download(self, url: str, dest_dir: Path, filename: str | None = None) -> list[Path]:
        """Download file(s) from url into dest_dir. Returns list of downloaded file paths."""
        ...
