"""Esperas cancelables y backoff acotado."""
import asyncio
import random


async def jitter(minimum: float, maximum: float) -> None:
    await asyncio.sleep(random.uniform(minimum, maximum))


def backoff(attempt: int, initial: float, maximum: float) -> float:
    ceiling = min(maximum, initial * 2 ** min(attempt, 30))
    return random.uniform(ceiling / 2, ceiling)
