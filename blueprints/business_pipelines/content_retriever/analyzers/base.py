from abc import ABC, abstractmethod
from pathlib import Path


class BaseAnalyzer(ABC):
    """Abstract base class for all analyzers."""

    @abstractmethod
    def analyze_image(self, image_path: Path, prompt: str = "Describe this image in detail.") -> str: ...

    @abstractmethod
    def analyze_text(self, text: str) -> str: ...

    @abstractmethod
    def analyze_video(self, video_path: Path) -> str: ...
