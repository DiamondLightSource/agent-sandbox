"""The host side of ``claude-sandbox``: the project-container launcher.

A port of ``container/claude-container``. ``options`` parses the launcher's
own options, ``launcher`` talks to podman or docker, and ``commands`` holds
the commands that run on the host.
"""
