"""The one exception the launch path raises for a refusal.

Its message is the text the bash shadow prints to stderr for the same
refusal, so the Python shadow can print ``str(error)`` unchanged.
"""


class SandboxError(Exception):
    """A configuration or invocation the sandbox refuses to launch with."""
