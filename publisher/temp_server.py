"""Temporary HTTP server for serving image files to Meta Graph API."""

import logging
import mimetypes
import os
import socket
import threading
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)


class _AllowedFilesHandler(SimpleHTTPRequestHandler):
    """HTTP handler that serves only explicitly allowed files."""

    # Set by the factory function below
    _allowed_filenames: set[str] = set()
    _serve_directory: str = ""

    def translate_path(self, path: str) -> str:
        """Resolve request path — only allow permitted filenames."""
        # Strip query string and fragment
        path = path.split("?", 1)[0].split("#", 1)[0]
        # Normalize: remove leading slash, take only the basename
        filename = os.path.basename(path.lstrip("/"))

        if filename not in self._allowed_filenames:
            return ""  # Will cause a 404

        return os.path.join(self._serve_directory, filename)

    def do_GET(self) -> None:
        """Serve the file if it matches, otherwise return 404."""
        file_path = self.translate_path(self.path)

        if not file_path or not os.path.isfile(file_path):
            self.send_error(HTTPStatus.NOT_FOUND, "File not found")
            return

        # Determine content type
        content_type, _ = mimetypes.guess_type(file_path)
        if content_type is None:
            content_type = "application/octet-stream"

        try:
            with open(file_path, "rb") as f:
                data = f.read()
        except OSError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Cannot read file")
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def list_directory(self, path: str) -> None:
        """Disable directory listings."""
        self.send_error(HTTPStatus.FORBIDDEN, "Directory listing disabled")
        return None

    def log_message(self, format: str, *args: object) -> None:
        """Route access logs through the module logger instead of stderr."""
        logger.debug("HTTP: %s", format % args)


def _make_handler(serve_directory: str, allowed_filenames: set[str]) -> type:
    """Create a handler class bound to a specific directory and filenames."""

    class BoundHandler(_AllowedFilesHandler):
        _allowed_filenames = allowed_filenames
        _serve_directory = serve_directory

    return BoundHandler


class TempImageServer:
    """Context manager that serves one or more image files via ThreadingHTTPServer.

    Usage (single file):
        with TempImageServer("/path/to/image.jpg", port=9876) as url:
            # url is like "http://1.2.3.4:9876/image.jpg"

    Usage (multiple files):
        with TempImageServer(["/path/a.jpg", "/path/b.jpg"], port=9876) as url:
            # url is the base URL; individual files at url + "/a.jpg", url + "/b.jpg"
            # Use server.get_url(path) for individual file URLs
    """

    def __init__(self, image_paths: str | list[str], port: int) -> None:
        """Initialize the server for one or more image files and a port."""
        if isinstance(image_paths, str):
            image_paths = [image_paths]

        if not image_paths:
            raise ValueError("At least one image path must be provided.")

        self._image_paths = [os.path.abspath(p) for p in image_paths]
        self._port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

        # Validate all files exist
        for path in self._image_paths:
            if not os.path.isfile(path):
                raise FileNotFoundError(f"Image file does not exist: {path}")

        # All files must be in the same directory
        directories = {os.path.dirname(p) for p in self._image_paths}
        if len(directories) != 1:
            raise ValueError(
                "All image files must be in the same directory for serving. "
                f"Found directories: {directories}"
            )

        self._directory = directories.pop()
        self._filenames = {os.path.basename(p) for p in self._image_paths}
        # For backwards compatibility, expose the first filename
        self._primary_filename = os.path.basename(self._image_paths[0])

        # Determine public base URL
        public_url = os.getenv("PUBLIC_URL")
        public_ip = os.getenv("PUBLIC_IP")
        if public_url:
            self._base_url = f"https://{public_url}"
            logger.info("Using PUBLIC_URL for image serving: %s", self._base_url)
        elif public_ip:
            self._base_url = f"http://{public_ip}:{self._port}"
        else:
            fallback_ip = socket.gethostbyname(socket.gethostname())
            self._base_url = f"http://{fallback_ip}:{self._port}"
            logger.warning(
                "Neither PUBLIC_URL nor PUBLIC_IP env var set — falling back to %s. "
                "This will likely not work for Meta Graph API in production "
                "(Meta needs a publicly reachable URL).",
                self._base_url,
            )

    @property
    def url(self) -> str:
        """Return the public URL for the primary (first) image."""
        return f"{self._base_url}/{self._primary_filename}"

    def get_url(self, image_path: str) -> str:
        """Return the public URL for a specific image file by its path."""
        filename = os.path.basename(image_path)
        if filename not in self._filenames:
            raise ValueError(f"File '{filename}' is not being served by this server.")
        return f"{self._base_url}/{filename}"

    def __enter__(self) -> str:
        """Start the HTTP server in a daemon thread and return the primary URL."""
        handler_class = _make_handler(self._directory, self._filenames)
        self._server = ThreadingHTTPServer(("0.0.0.0", self._port), handler_class)

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name=f"temp-image-server-{self._port}",
        )
        self._thread.start()

        logger.info(
            "Temp image server started on port %d — serving %d file(s): %s",
            self._port,
            len(self._filenames),
            ", ".join(sorted(self._filenames)),
        )
        return self.url

    def __exit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: object) -> None:
        """Shut down the HTTP server and release the socket."""
        if self._server is not None:
            self._server.shutdown()  # Signals serve_forever to stop (blocks)
            self._server.server_close()  # Releases the socket
            logger.info("Temp image server stopped on port %d.", self._port)

        self._server = None
        self._thread = None
