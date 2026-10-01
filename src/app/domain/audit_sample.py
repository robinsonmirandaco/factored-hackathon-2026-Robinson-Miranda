"""Audit sample of the cases the system resolved on its own (TRZ-29, design 6.7).

A fraction rho of those cases goes to the analyst queue, drawn at random with a recorded seed.
Draw n depends only on the seed and on n, so anyone can recompute it from the two numbers its
audit row keeps: the n-th draw with the same seed is always the same.
"""

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Draw:
    """One audit sample draw.

    Attributes:
        seed: Seed of the policy.
        n: Number of the draw.
        rho: Sample rate of the policy.
        u: The uniform number drawn, in [0, 1).
        selected: The case goes to the queue as an audit sample.
    """

    seed: int
    n: int
    rho: float
    u: float
    selected: bool


def draw(seed: int, n: int, rho: float) -> Draw:
    """Draws the n-th audit sample decision.

    Args:
        seed: Seed of the policy, `autonomy.audit_sample_seed`.
        n: Number of the draw, from 1.
        rho: Sample rate, `autonomy.audit_sample_rate`.

    Returns:
        The draw; selected when u < rho.
    """
    # A string seed is hashed with SHA-512 by random.Random, which is stable across platforms
    # and Python versions.
    u = random.Random(f"{seed}:{n}").random()
    return Draw(seed=seed, n=n, rho=rho, u=u, selected=u < rho)
