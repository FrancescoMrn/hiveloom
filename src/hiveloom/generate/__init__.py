"""Generation: a strong model drives the construction API to build a harness."""

from hiveloom.generate.generator import generate as generate_harness
from hiveloom.generate.llm import FakeStrongModel, StrongModel

__all__ = ["FakeStrongModel", "StrongModel", "generate_harness"]
