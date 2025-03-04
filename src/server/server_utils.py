""" Utility functions for the server. """

import asyncio
import math
import psycopg2
import shutil
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import Response
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.middleware.base import BaseHTTPMiddleware

from gitingest.config import TMP_BASE_PATH
from server.server_config import DELETE_REPO_AFTER, DB_CONFIG

# Initialize a rate limiter
limiter = Limiter(key_func=get_remote_address)


async def rate_limit_exception_handler(request: Request, exc: Exception) -> Response:
    """
    Custom exception handler for rate-limiting errors.

    Parameters
    ----------
    request : Request
        The incoming HTTP request.
    exc : Exception
        The exception raised, expected to be RateLimitExceeded.

    Returns
    -------
    Response
        A response indicating that the rate limit has been exceeded.

    Raises
    ------
    exc
        If the exception is not a RateLimitExceeded error, it is re-raised.
    """
    if isinstance(exc, RateLimitExceeded):
        # Delegate to the default rate limit handler
        return _rate_limit_exceeded_handler(request, exc)
    # Re-raise other exceptions
    raise exc


# Database connection function
def get_db_connection():
    """
    Get a connection to the PostgreSQL database.
    
    Returns
    -------
    connection
        A psycopg2 connection object.
    """
    return psycopg2.connect(**DB_CONFIG)

# Initialize database
def init_db():
    """
    Initialize the database by creating the necessary tables if they don't exist.
    """
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS api_logs (
            id SERIAL PRIMARY KEY,
            timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            level VARCHAR(10),
            message TEXT,
            method VARCHAR(10) NULL,
            path TEXT NULL,
            status_code INTEGER NULL,
            ip_address VARCHAR(45) NULL,
            processing_time FLOAT NULL
        );
        """)
        conn.commit()
        cursor.close()
        conn.close()
        print("Database initialized successfully")
    except Exception as e:
        print(f"Error initializing database: {e}")

# Logging middleware
class LoggingMiddleware(BaseHTTPMiddleware):
    """
    Middleware for logging FastAPI requests to PostgreSQL database.
    """
    async def dispatch(self, request, call_next):
        """
        Process the request and log information to the database.
        
        Parameters
        ----------
        request : Request
            The incoming HTTP request.
        call_next : Callable
            The next middleware or route handler in the chain.
            
        Returns
        -------
        Response
            The response from the next middleware or route handler.
        """
        start_time = time.time()
        
        # Process the request
        response = await call_next(request)
        
        # Calculate processing time
        process_time = time.time() - start_time
        
        # Log to database
        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            
            # Extract request information
            method = request.method
            path = request.url.path
            status_code = response.status_code
            client_host = request.client.host if request.client else None
            
            # Create log message similar to FastAPI's default format
            message = f"{client_host} - \"{method} {path} HTTP/1.1\" {status_code}"
            
            cursor.execute(
                "INSERT INTO api_logs (level, message, method, path, status_code, ip_address, processing_time) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                ("INFO", message, method, path, status_code, client_host, process_time)
            )
            conn.commit()
            cursor.close()
            conn.close()
        except Exception as e:
            print(f"Error logging to database: {e}")
            
        return response

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifecycle manager for handling startup and shutdown events for the FastAPI application.

    Parameters
    ----------
    app : FastAPI
        The FastAPI application instance.

    Yields
    -------
    None
        Yields control back to the FastAPI application while the background task runs.
    """
    # Initialize the database
    init_db()
    
    # Start the repository cleanup task
    task = asyncio.create_task(_remove_old_repositories())

    yield
    # Cancel the background task on shutdown
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _remove_old_repositories():
    """
    Periodically remove old repository folders.

    Background task that runs periodically to clean up old repository directories.

    This task:
    - Scans the TMP_BASE_PATH directory every 60 seconds
    - Removes directories older than DELETE_REPO_AFTER seconds
    - Before deletion, logs repository URLs to history.txt if a matching .txt file exists
    - Handles errors gracefully if deletion fails

    The repository URL is extracted from the first .txt file in each directory,
    assuming the filename format: "owner-repository.txt"
    """
    while True:
        try:
            if not TMP_BASE_PATH.exists():
                await asyncio.sleep(60)
                continue

            current_time = time.time()

            for folder in TMP_BASE_PATH.iterdir():
                # Skip if folder is not old enough
                if current_time - folder.stat().st_ctime <= DELETE_REPO_AFTER:
                    continue

                await _process_folder(folder)

        except Exception as exc:
            print(f"Error in _remove_old_repositories: {exc}")

        await asyncio.sleep(60)


async def _process_folder(folder: Path) -> None:
    """
    Process a single folder for deletion and logging.

    Parameters
    ----------
    folder : Path
        The path to the folder to be processed.
    """
    # Try to log repository URL before deletion
    try:
        txt_files = [f for f in folder.iterdir() if f.suffix == ".txt"]

        # Extract owner and repository name from the filename
        filename = txt_files[0].stem
        if txt_files and "-" in filename:
            owner, repo = filename.split("-", 1)
            repo_url = f"{owner}/{repo}"

            with open("history.txt", mode="a", encoding="utf-8") as history:
                history.write(f"{repo_url}\n")

    except Exception as exc:
        print(f"Error logging repository URL for {folder}: {exc}")

    # Delete the folder
    try:
        shutil.rmtree(folder)
    except Exception as exc:
        print(f"Error deleting {folder}: {exc}")


def log_slider_to_size(position: int) -> int:
    """
    Convert a slider position to a file size in bytes using a logarithmic scale.

    Parameters
    ----------
    position : int
        Slider position ranging from 0 to 500.

    Returns
    -------
    int
        File size in bytes corresponding to the slider position.
    """
    maxp = 500
    minv = math.log(1)
    maxv = math.log(102_400)
    return round(math.exp(minv + (maxv - minv) * pow(position / maxp, 1.5))) * 1024


def is_browser(request: Request) -> bool:
    """
    Detect if the request is coming from a browser based on the User-Agent header.
    
    Parameters
    ----------
    request : Request
        The incoming HTTP request.
        
    Returns
    -------
    bool
        True if the request is from a browser, False otherwise.
    """
    user_agent = request.headers.get("user-agent", "").lower()
    browser_identifiers = ["mozilla", "chrome", "safari", "edge", "firefox", "webkit", "opera"]
    
    # Check if any browser identifier is in the user agent string
    return any(identifier in user_agent for identifier in browser_identifiers)


## Color printing utility
class Colors:
    """ANSI color codes"""

    BLACK = "\033[0;30m"
    RED = "\033[0;31m"
    GREEN = "\033[0;32m"
    BROWN = "\033[0;33m"
    BLUE = "\033[0;34m"
    PURPLE = "\033[0;35m"
    CYAN = "\033[0;36m"
    LIGHT_GRAY = "\033[0;37m"
    DARK_GRAY = "\033[1;30m"
    LIGHT_RED = "\033[1;31m"
    LIGHT_GREEN = "\033[1;32m"
    YELLOW = "\033[1;33m"
    LIGHT_BLUE = "\033[1;34m"
    LIGHT_PURPLE = "\033[1;35m"
    LIGHT_CYAN = "\033[1;36m"
    WHITE = "\033[1;37m"
    BOLD = "\033[1m"
    FAINT = "\033[2m"
    ITALIC = "\033[3m"
    UNDERLINE = "\033[4m"
    BLINK = "\033[5m"
    NEGATIVE = "\033[7m"
    CROSSED = "\033[9m"
    END = "\033[0m"
