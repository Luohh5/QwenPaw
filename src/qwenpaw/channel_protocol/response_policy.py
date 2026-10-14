# -*- coding: utf-8 -*-
"""Immutable, route-specific optional response operations."""

from collections.abc import Mapping
from dataclasses import dataclass
from .errors import ProtocolValidationError


@dataclass(frozen=True)
class ResponsePolicy:
    streaming: bool = False
    prepare: bool = False
    completion_reaction: bool = False

    def __post_init__(self):
        if any(
            type(getattr(self, key)) is not bool
            for key in self.__dataclass_fields__
        ):
            raise ProtocolValidationError("Response policy requires booleans")

    @classmethod
    def from_mapping(cls, value):
        if not isinstance(value, Mapping) or set(value) != set(
            cls.__dataclass_fields__,
        ):
            raise ProtocolValidationError("Invalid response policy fields")
        return cls(**value)

    def to_mapping(self):
        return {key: getattr(self, key) for key in self.__dataclass_fields__}
