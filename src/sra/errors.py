class SRAError(Exception):
    """An actionable, safe-to-display application error."""


class CompatibilityError(SRAError):
    """A required CLI surface or terminal event is unavailable."""


class ConflictError(SRAError):
    """An approval, execution lease, or repository invariant conflicts."""


class ProcessError(SRAError):
    """A child failed, timed out, exceeded its output limit, or was cancelled."""


class GitHubError(SRAError):
    def __init__(self, status: int, message: str):
        self.status = status
        super().__init__(f"GitHub HTTP {status}: {message}")
