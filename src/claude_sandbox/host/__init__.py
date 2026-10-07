"""The host side of ``claude-sandbox``: the project-container launcher.

Once the bash ``claude-container``, which 5.0 replaced (ADR 26).
``options`` parses the launcher's own options, ``launcher`` talks to podman
or docker, and ``commands`` holds the commands that run on the host.
"""
