"""The one exception the launch path raises for a refusal.

Its message is the text the shadow prints to stderr for the refusal, so it
can print ``str(error)`` unchanged.
"""


class SandboxError(Exception):
    """A configuration or invocation the sandbox refuses to launch with."""
