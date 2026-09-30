"""CSV files that are safe to open in Excel, LibreOffice or Google Sheets.

A cell that starts with = + - @ (or a tab / carriage return) is treated as a formula by
spreadsheet programs, so user text such as a task title "=HYPERLINK(...)" could run when
someone opens an export. Such cells get a leading apostrophe; plain numbers are left alone.
Don't use this for files read by machines (e.g. assembly-house BOM/CPL files).
"""
import csv
import re

_NUMBER = re.compile(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$")
_RISKY = ("=", "+", "-", "@", "\t", "\r")


def cell(value):
    if isinstance(value, str) and value.startswith(_RISKY) and not _NUMBER.match(value.strip()):
        return "'" + value
    return value


class _SafeWriter:
    def __init__(self, writer):
        self._w = writer

    def writerow(self, row):
        return self._w.writerow([cell(v) for v in row])

    def writerows(self, rows):
        for row in rows:
            self.writerow(row)


def writer(fileobj, *args, **kwargs):
    return _SafeWriter(csv.writer(fileobj, *args, **kwargs))
