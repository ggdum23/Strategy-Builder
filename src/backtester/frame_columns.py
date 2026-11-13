"""Compact columns for a prepared series.

Low-cardinality text is a categorical code. A canonical ``timestamp_et`` is
five small integer arrays, formatted only when a caller reads one stamp.
Numeric columns stay memory-mappable. The values a strategy or a report
reads are the same strings and numbers as a fully materialized frame.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pandas.api.extensions import ExtensionArray, ExtensionDtype

# ``HH:MM`` for minute 0..1439. Categorical codes for ``time`` are minutes.
CLOCK_TEXT = tuple(f"{minute // 60:02d}:{minute % 60:02d}" for minute in range(24 * 60))


class StampDtype(ExtensionDtype):
    name = "stamp"
    type = str
    na_value = None

    @classmethod
    def construct_array_type(cls):
        return StampArray


class StampArray(ExtensionArray):
    """Canonical ``YYYY-MM-DDTHH:MM:SS±HH:MM`` stamps, stored as components.

    ``offset`` is minutes east of UTC. Seconds are 00. One row formats to the
    same text ``datetime.isoformat`` produces for that stamp.
    """

    _dtype = StampDtype()

    def __init__(self, year, month, day, minute, offset):
        self._year = np.asarray(year, dtype=np.int16)
        self._month = np.asarray(month, dtype=np.int8)
        self._day = np.asarray(day, dtype=np.int8)
        self._minute = np.asarray(minute, dtype=np.int16)
        self._offset = np.asarray(offset, dtype=np.int16)
        n = len(self._year)
        if not all(len(part) == n for part in (self._month, self._day, self._minute, self._offset)):
            raise ValueError("stamp components differ in length")

    def __reduce__(self):
        return (StampArray, (self._year, self._month, self._day, self._minute, self._offset))

    @property
    def dtype(self):
        return self._dtype

    def __len__(self):
        return len(self._year)

    @property
    def nbytes(self):
        return (
            self._year.nbytes
            + self._month.nbytes
            + self._day.nbytes
            + self._minute.nbytes
            + self._offset.nbytes
        )

    def _format(self, index: int) -> str:
        offset = int(self._offset[index])
        sign = "+" if offset >= 0 else "-"
        hours, minutes = divmod(abs(offset), 60)
        minute = int(self._minute[index])
        hour, minute_of = divmod(minute, 60)
        return (
            f"{int(self._year[index]):04d}-{int(self._month[index]):02d}-{int(self._day[index]):02d}"
            f"T{hour:02d}:{minute_of:02d}:00{sign}{hours:02d}:{minutes:02d}"
        )

    def __getitem__(self, key):
        if isinstance(key, (int, np.integer)):
            return self._format(int(key))
        return StampArray(
            self._year[key],
            self._month[key],
            self._day[key],
            self._minute[key],
            self._offset[key],
        )

    def __iter__(self):
        for index in range(len(self)):
            yield self._format(index)

    def isna(self):
        return np.zeros(len(self), dtype=bool)

    def take(self, indices, *, allow_fill=False, fill_value=None):
        return self[np.asarray(indices)]

    def copy(self):
        return StampArray(
            self._year.copy(),
            self._month.copy(),
            self._day.copy(),
            self._minute.copy(),
            self._offset.copy(),
        )

    @classmethod
    def _from_sequence(cls, scalars, *, dtype=None, copy=False):
        year = np.empty(len(scalars), dtype=np.int16)
        month = np.empty(len(scalars), dtype=np.int8)
        day = np.empty(len(scalars), dtype=np.int8)
        minute = np.empty(len(scalars), dtype=np.int16)
        offset = np.empty(len(scalars), dtype=np.int16)
        for index, value in enumerate(scalars):
            text = str(value)
            if len(text) != 25 or text[10] != "T" or text[19] not in "+-":
                raise TypeError("stamp text is not canonical")
            year[index] = int(text[0:4])
            month[index] = int(text[5:7])
            day[index] = int(text[8:10])
            minute[index] = int(text[11:13]) * 60 + int(text[14:16])
            sign = -1 if text[19] == "-" else 1
            offset[index] = sign * (int(text[20:22]) * 60 + int(text[23:25]))
        return cls(year, month, day, minute, offset)

    @classmethod
    def _concat_same_type(cls, to_concat):
        return cls(
            np.concatenate([item._year for item in to_concat]),
            np.concatenate([item._month for item in to_concat]),
            np.concatenate([item._day for item in to_concat]),
            np.concatenate([item._minute for item in to_concat]),
            np.concatenate([item._offset for item in to_concat]),
        )


def is_stamp(series) -> bool:
    return getattr(getattr(series, "dtype", None), "name", "") == "stamp"


def categorical_codes(series):
    """``(codes, labels)`` for a categorical or a plain text column."""
    if isinstance(series.dtype, pd.CategoricalDtype):
        labels = [str(value) for value in series.cat.categories]
        return series.cat.codes.to_numpy(copy=False), labels
    codes, uniques = pd.factorize(series.astype(str), sort=False)
    return codes, [str(value) for value in uniques]


def take_labels(labels, codes) -> np.ndarray:
    """Object array of the selected labels. The strings themselves are shared."""
    table = np.asarray(list(labels), dtype=object)
    return np.take(table, np.asarray(codes))
