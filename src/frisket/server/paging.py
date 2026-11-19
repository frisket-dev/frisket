"""FastAPI query parameter types for public offset-style page envelopes."""

from __future__ import annotations

from typing import Annotated

from fastapi import Query

PageOffset = Annotated[int, Query(ge=0)]
PageLimit100 = Annotated[int, Query(ge=1, le=100)]
PageLimit500 = Annotated[int, Query(ge=1, le=500)]
