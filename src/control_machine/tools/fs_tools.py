"""Download a URL onto this Mac. Host files otherwise use Deep Agents ls/read_file."""

from __future__ import annotations

import httpx
from langchain_core.tools import BaseTool, tool

from ..config import Settings
from ..fs import FsError, extra_deny_prefixes, filesystem_root, host_path_from_virtual


def build_fs_tools(settings: Settings) -> list[BaseTool]:
    root = filesystem_root(settings)
    deny = extra_deny_prefixes(settings)
    max_download = settings.filesystem_download_max_bytes

    @tool
    async def fs_download(url: str, dest: str) -> str:
        """Download a URL onto this Mac (not into the Docker Chrome profile).

        Args:
            url: http(s) URL.
            dest: Destination under /home (this Mac's home), e.g. /home/Downloads/file.pdf.
        """
        try:
            target = host_path_from_virtual(dest, root=root, extra_deny=deny)
        except FsError as exc:
            return str(exc)
        if target is None:
            return "Destination must be under /home (this Mac's home directory)."
        if not url.startswith(("http://", "https://")):
            return "URL must start with http:// or https://"
        written = 0
        too_large = False
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            async with httpx.AsyncClient(follow_redirects=True, timeout=30.0) as client:
                async with client.stream("GET", url) as response:
                    response.raise_for_status()
                    with target.open("wb") as handle:
                        async for chunk in response.aiter_bytes():
                            written += len(chunk)
                            if written > max_download:
                                too_large = True
                                break
                            handle.write(chunk)
        except httpx.HTTPError as exc:
            return f"Download failed: {exc}"
        except OSError as exc:
            return f"Could not write {target}: {exc}"
        if too_large:
            target.unlink(missing_ok=True)
            return f"Download exceeded {max_download} bytes; aborted."
        return f"Downloaded {written} bytes to {target}"

    return [fs_download]
