from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class Description:
    """Holds description text for lessons or courses."""
    text: str
    description_type: str

@dataclass
class AuxiliaryURL:
    """Holds auxiliary URL information."""
    url_id: str
    url: str
    order: int
    title: str
    description: str

@dataclass
class Video:
    """Represents a video to be downloaded."""
    video_id: str
    url: str
    order: int
    title: str
    size: int
    duration: int
    extra_props: dict = field(default_factory=dict)
    
@dataclass
class Attachment:
    """Represents a file attachment to be downloaded."""
    attachment_id: str
    url: str
    filename: str
    order: int
    extension: str
    size: int

@dataclass
class LessonContent:
    """Holds all downloadable content for a single lesson."""
    description: Optional[Description] = None
    auxiliary_urls: List[AuxiliaryURL] = field(default_factory=list)
    videos: List[Video] = field(default_factory=list)
    attachments: List[Attachment] = field(default_factory=list)
