"""Line-oriented file editing primitives used by the filesystem tools."""

from __future__ import annotations

import re
from pathlib import Path
from typing import List


class FileEditor:
    @staticmethod
    def read_lines(file_path: Path) -> List[str]:
        return file_path.read_text(encoding="utf-8").splitlines()

    @staticmethod
    def write_lines(file_path: Path, lines: List[str]):
        file_path.write_text("\n".join(lines), encoding="utf-8")

    @classmethod
    def replace_lines(
        cls, file_path: Path, start_line: int, end_line: int, new_content: str
    ):
        lines = cls.read_lines(file_path)
        total = len(lines)
        start_line = max(1, start_line)
        end_line = min(total, end_line)
        if start_line > end_line:
            return {
                "success": False,
                "message": f"Invalid range: {start_line}-{end_line}",
            }
        replaced = lines[start_line - 1 : end_line]
        new_lines = new_content.splitlines()
        final = lines[: start_line - 1] + new_lines + lines[end_line:]
        cls.write_lines(file_path, final)
        return {
            "success": True,
            "message": f"Replaced lines {start_line}-{end_line}",
            "replaced_content": "\n".join(replaced),
            "lines_before": total,
            "lines_after": len(final),
        }

    @classmethod
    def insert_at_line(
        cls, file_path: Path, line_number: int, content: str, mode: str = "before"
    ):
        lines = cls.read_lines(file_path)
        total = len(lines)
        line_number = max(1, min(line_number, total + 1))
        idx = line_number - 1 if mode == "before" else line_number
        new_lines = content.splitlines()
        final = lines[:idx] + new_lines + lines[idx:]
        cls.write_lines(file_path, final)
        return {
            "success": True,
            "message": f"Inserted {len(new_lines)} lines at {line_number} ({mode})",
            "lines_before": total,
            "lines_after": len(final),
        }

    @classmethod
    def delete_lines(cls, file_path: Path, start_line: int, end_line: int):
        lines = cls.read_lines(file_path)
        total = len(lines)
        start_line = max(1, start_line)
        end_line = min(total, end_line)
        deleted = lines[start_line - 1 : end_line]
        final = lines[: start_line - 1] + lines[end_line:]
        cls.write_lines(file_path, final)
        return {
            "success": True,
            "message": f"Deleted lines {start_line}-{end_line}",
            "deleted_content": "\n".join(deleted),
            "lines_before": total,
            "lines_after": len(final),
        }

    @classmethod
    def find_and_replace(
        cls,
        file_path: Path,
        search: str,
        replace: str,
        use_regex=False,
        case_sensitive=True,
        replace_all=True,
    ):
        content = file_path.read_text(encoding="utf-8")
        if use_regex:
            flags = 0 if case_sensitive else re.IGNORECASE
            pattern = re.compile(search, flags)
            count_func = lambda: len(pattern.findall(content))
            matches = count_func()
            if replace_all:
                new_content, count = pattern.subn(replace, content)
            else:
                new_content, count = pattern.subn(replace, content, count=1)
        else:
            if case_sensitive:
                matches = content.count(search)
                if replace_all:
                    new_content = content.replace(search, replace)
                    count = matches
                else:
                    new_content = content.replace(search, replace, 1)
                    count = min(1, matches)
            else:
                pattern = re.compile(re.escape(search), re.IGNORECASE)
                matches = len(pattern.findall(content))
                if replace_all:
                    new_content = pattern.sub(replace, content)
                    count = matches
                else:
                    new_content = pattern.sub(replace, content, count=1)
                    count = min(1, matches)

        if count > 0:
            file_path.write_text(new_content, encoding="utf-8")
        return {"success": True, "matches_found": matches, "replacements_made": count}
