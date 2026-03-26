"""Temporary HTTP server for serving a single image to Meta Graph API."""

import logging
import mimetypes
import os
import socket
import threading
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger(__name__)


class _SingleFileHandler(SimpleHTTPRequestHandler):
    """HTTP handler that serves exactly one file and rejects everything else."""

    # Set by the factory function below
    _allowed_filename: str = ""
    _serve_directory: str = ""

    def translate_path(self, path: str) -> str:
        """Resolve request path — only allow the single permitted filename."""
        # Strip query string and fragment
        path = path.split("?", 1)[0].split("#", 1)[0]
        # Normalize: remove leading slash, take only the basename
        filename = os.path.basename(path.lstrip("/"))

        if filename != self._allowed_filename:
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


def _make_handler(serve_directory: str, allowed_filename: str) -> type:
    """Create a handler class bound to a specific directory and filename."""

    class BoundHandler(_SingleFileHandler):
        _allowed_filename = allowed_filename
        _serve_directory = serve_directory

    return BoundHandler


class TempImageServer:
    """Context manager that serves a single image via ThreadingHTTPServer.

    Usage:
        with TempImageServer("/path/to/image.jpg", port=9876) as url:
            # url is like "http://1.2.3.4:9876/image.jpg"
            # Meta Graph API can fetch the image at this URL
    """

    def __init__(self, image_path: str, port: int) -> None:
        """Initialize the server for a specific image file and port."""
        self._image_path = os.path.abspath(image_path)
        self._port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

        if not os.path.isfile(self._image_path):
            raise FileNotFoundError(
                f"Image file does not exist: {self._image_path}"
            )

        self._directory = os.path.dirname(self._image_path)
        self._filename = os.path.basename(self._image_path)

        # Determine public IP
        public_ip = os.getenv("PUBLIC_IP")
        if public_ip:
            self._public_ip = public_ip
        else:
            self._public_ip = socket.gethostbyname(socket.gethostname())
            logger.warning(
                "PUBLIC_IP env var not set — falling back to %s. "
                "This will likely not work for Meta Graph API in production "
                "(Meta needs a publicly reachable IP).",
                self._public_ip,
            )

    @property
    def url(self) -> str:
        """Return the public URL for the served image."""
        return f"http://{self._public_ip}:{self._port}/{self._filename}"

    def __enter__(self) -> str:
        """Start the HTTP server in a daemon thread and return the public URL."""
        handler_class = _make_handler(self._directory, self._filename)
        self._server = ThreadingHTTPServer(("0.0.0.0", self._port), handler_class)

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name=f"temp-image-server-{self._port}",
        )
        self._thread.start()

        logger.info("Temp image server started on port %d — serving %s", self._port, self._filename)
        return self.url

    def __exit__(self, exc_type: type | None, exc_val: BaseException | None, exc_tb: object) -> None:
        """Shut down the HTTP server and release the socket."""
        if self._server is not None:
            self._server.shutdown()  # Signals serve_forever to stop (blocks)
            self._server.server_close()  # Releases the socket
            logger.info("Temp image server stopped on port %d.", self._port)

        self._server = None
        self._thread = None
