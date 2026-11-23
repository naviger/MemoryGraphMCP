from typing import Annotated

from pydantic import Field

MemoryKey = Annotated[
    str,
    Field(min_length=1, max_length=254, pattern=r"^[A-Za-z0-9_\-.$+!*'(),;=@%:]+$"),
]
