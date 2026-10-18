"""Bounded, informational checks for extracted document text.

The checks identify concrete reasons that a page may need manual review.  An
empty result does not establish that the text is complete or correct.
"""

from .quality import TextQualityFlag, assess_text_quality

__all__ = [
    "TextQualityFlag",
    "assess_text_quality",
]
