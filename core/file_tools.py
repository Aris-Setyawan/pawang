"""File Tools — safe file read/write/search operations for agents.

All operations are sandboxed to workspace directory.
"""

import os
import fnmatch
from pathlib import Path

from core.logger import log

# Allowed directories for file operations
WORKSPACE_DIR = Path("/root/pawang/workspace")
ALLOWED_ROOTS = [WORKSPACE_DIR, Path("/workspace"), Path("/tmp")]


def _is_allowed_path(path_str: str) -> tuple[bool, Path]:
    """Check if path is within allowed directories. Returns (ok, resolved_path)."""
    try:
        resolved = Path(path_str).resolve()
    except (ValueError, OSError):
        return False, Path()

    for allowed in ALLOWED_ROOTS:
        allowed_resolved = allowed.resolve()
        if str(resolved).startswith(str(allowed_resolved)):
            return True, resolved

    return False, resolved


def file_read(file_path: str, max_lines: int = 200, offset: int = 0) -> str:
    """Read a file from workspace. Returns content with line numbers."""
    ok, resolved = _is_allowed_path(file_path)
    if not ok:
        return f"Error: path not in allowed directory (workspace or /tmp)"

    if not resolved.exists():
        return f"Error: file not found: {file_path}"

    if not resolved.is_file():
        return f"Error: not a file: {file_path}"

    # Check file size
    size = resolved.stat().st_size
    if size > 1_000_000:  # 1MB limit
        return f"Error: file too large ({size:,} bytes). Use offset/max_lines to read portions."

    try:
        lines = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)
        subset = lines[offset:offset + max_lines]

        result_lines = []
        for i, line in enumerate(subset, start=offset + 1):
            result_lines.append(f"{i:4d} | {line}")

        header = f"File: {resolved} ({total} lines total)"
        if offset > 0 or offset + max_lines < total:
            header += f" [showing lines {offset+1}-{min(offset+max_lines, total)}]"

        return header + "\n" + "\n".join(result_lines)
    except Exception as e:
        return f"Error reading file: {e}"


def file_write(file_path: str, content: str) -> str:
    """Write content to a file in workspace."""
    ok, resolved = _is_allowed_path(file_path)
    if not ok:
        return f"Error: path not in allowed directory (workspace or /tmp)"

    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return f"Written {len(content)} chars to {resolved}"
    except Exception as e:
        return f"Error writing file: {e}"


def file_patch(file_path: str, old_text: str, new_text: str) -> str:
    """Replace text in a file (exact string match)."""
    ok, resolved = _is_allowed_path(file_path)
    if not ok:
        return f"Error: path not in allowed directory"

    if not resolved.exists():
        return f"Error: file not found: {file_path}"

    try:
        content = resolved.read_text(encoding="utf-8")
        if old_text not in content:
            return f"Error: old_text not found in file"

        count = content.count(old_text)
        new_content = content.replace(old_text, new_text, 1)
        resolved.write_text(new_content, encoding="utf-8")
        return f"Patched {resolved} (1 of {count} occurrences replaced)"
    except Exception as e:
        return f"Error patching file: {e}"


def file_search(pattern: str, directory: str = "") -> str:
    """Search for files by glob pattern in workspace."""
    search_dir = Path(directory) if directory else WORKSPACE_DIR
    ok, resolved = _is_allowed_path(str(search_dir))
    if not ok:
        search_dir = WORKSPACE_DIR

    matches = []
    try:
        for root, dirs, files in os.walk(search_dir):
            # Skip hidden dirs
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for filename in files:
                if fnmatch.fnmatch(filename, pattern):
                    full = os.path.join(root, filename)
                    size = os.path.getsize(full)
                    matches.append(f"  {full} ({size:,} bytes)")
                    if len(matches) >= 50:
                        break
            if len(matches) >= 50:
                break
    except Exception as e:
        return f"Error searching: {e}"

    if not matches:
        return f"No files matching '{pattern}' in {search_dir}"

    return f"Found {len(matches)} files:\n" + "\n".join(matches)


def web_fetch(url: str) -> str:
    """Fetch a URL and extract text content. Synchronous wrapper."""
    import asyncio
    return asyncio.get_event_loop().run_until_complete(_web_fetch_async(url))


async def web_fetch_async(url: str, max_chars: int = 8000) -> str:
    """Fetch a URL and extract readable text content."""
    import httpx

    if not url.startswith(("http://", "https://")):
        return "Error: URL must start with http:// or https://"

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url, headers={
                "User-Agent": "Mozilla/5.0 (compatible; PawangBot/1.0)",
            })
            resp.raise_for_status()

        content_type = resp.headers.get("content-type", "")

        if "application/json" in content_type:
            return resp.text[:max_chars]

        if "text/plain" in content_type:
            return resp.text[:max_chars]

        # HTML — extract text
        html = resp.text
        text = _extract_text_from_html(html)
        if len(text) > max_chars:
            text = text[:max_chars] + "\n...(truncated)"
        return text

    except httpx.HTTPStatusError as e:
        return f"HTTP error: {e.response.status_code}"
    except Exception as e:
        return f"Fetch error: {e}"


def _extract_text_from_html(html: str) -> str:
    """Simple HTML to text extraction without external dependencies."""
    import re

    # Remove script and style blocks
    text = re.sub(r'<script[^>]*>[\s\S]*?</script>', '', html, flags=re.I)
    text = re.sub(r'<style[^>]*>[\s\S]*?</style>', '', text, flags=re.I)
    # Remove comments
    text = re.sub(r'<!--[\s\S]*?-->', '', text)
    # Remove tags
    text = re.sub(r'<[^>]+>', ' ', text)
    # Decode common entities
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()
    # Split into lines for readability
    sentences = re.split(r'(?<=[.!?])\s+', text)
    return "\n".join(sentences)
