"""
spinner.py

Zero-dependency terminal spinner for long-running operations.
Writes to stderr so stdout stays clean for machine-parseable output.
"""

import sys
import threading
import time
import itertools


class Spinner:
    """Context manager that shows an animated spinner on stderr during LLM calls.

    Usage:
        with Spinner("Reasoning through protocol..."):
            response = client.messages.create(...)

    No-op when stderr is not a TTY (piped output, CI environments).
    """

    # Braille dots — smooth animation in most modern terminals
    BRAILLE = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    # ASCII fallback for terminals that can't render braille
    ASCII = ["|", "/", "-", "\\"]

    def __init__(self, message: str, stream=None):
        self.message = message
        self.stream = stream or sys.stderr
        self._active = False
        self._thread = None
        self._lock = threading.Lock()

        # Determine if we can animate
        self._is_tty = hasattr(self.stream, 'isatty') and self.stream.isatty()

        # Pick character set based on encoding
        try:
            encoding = getattr(self.stream, 'encoding', 'utf-8') or 'utf-8'
            if 'utf' in encoding.lower():
                self._chars = self.BRAILLE
            else:
                self._chars = self.ASCII
        except Exception:
            self._chars = self.ASCII

    def __enter__(self):
        if not self._is_tty:
            # Non-TTY: just print the message once, no animation
            self.stream.write(f"  {self.message}\n")
            self.stream.flush()
            return self

        self._active = True
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self._active = False
        if self._thread:
            self._thread.join(timeout=1.0)
        if self._is_tty:
            # Clear the spinner line
            self.stream.write(f"\r{' ' * (len(self.message) + 10)}\r")
            self.stream.flush()
        return False  # Don't suppress exceptions

    def update(self, message: str):
        """Swap the animated message mid-run (thread-safe).

        No-op on non-TTY streams, matching __enter__ (no animation thread runs).
        The _spin loop re-reads self.message each tick, so the change surfaces
        on the next frame.
        """
        if not self._is_tty:
            return
        with self._lock:
            self.message = message

    def _spin(self):
        for char in itertools.cycle(self._chars):
            if not self._active:
                break
            with self._lock:
                message = self.message
            # \033[K clears from the cursor to end of line, so a shorter message
            # replacing a longer one doesn't leave stale tail characters.
            self.stream.write(f"\r\033[K  {char} {message}")
            self.stream.flush()
            time.sleep(0.08)
