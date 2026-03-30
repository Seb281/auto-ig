"""Data contracts for the auto-ig agent pipeline.

All dataclasses are defined here as the single canonical source.
Every module imports from `agents` — never redefine these elsewhere.
"""

from dataclasses import dataclass, field


@dataclass
class PlannerBrief:
    """Output of the Content Planner agent."""

    topic: str
    angle: str
    visual_keywords: list[str]
    mood: str
    content_pillar: str
    content_type: str = "single_image"  # "single_image" or "carousel"


@dataclass
class ImageResult:
    """Output of the Image Sourcing agent (single image)."""

    local_path: str
    source: str  # "user" | "unsplash" | "pexels" | "gemini"
    phash: str
    score: float  # 0.0–1.0


@dataclass
class CaptionResult:
    """Output of the Caption Writer agent."""

    caption: str
    hashtags: list[str]
    alt_text: str


@dataclass
class ReviewResult:
    """Output of the Reviewer agent."""

    status: str  # "PASS" | "FAIL"
    reasons: list[str]
    retry_type: str | None  # "image" | "caption" | None


@dataclass
class PipelineResult:
    """End-to-end result of a single pipeline run."""

    success: bool
    post_id: str | None
    brief: PlannerBrief | None
    image: ImageResult | None
    caption: CaptionResult | None
    review: ReviewResult | None
    error: str | None
    skipped: bool = field(default=False)
    # Carousel support: list of ImageResult for multi-image posts
    images: list[ImageResult] = field(default_factory=list)
