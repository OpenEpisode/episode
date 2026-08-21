from typing import Annotated

from fastapi import Query

DEFAULT_LIMIT = 100
MAX_LIMIT = 500

PageLimit = Annotated[
    int,
    Query(
        ge=1,
        le=MAX_LIMIT,
        description="Maximum number of items to return.",
    ),
]
PageOffset = Annotated[
    int,
    Query(
        ge=0,
        description="Number of items to skip in the stable result order.",
    ),
]
