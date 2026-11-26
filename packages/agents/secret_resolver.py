"""Server-only resolver for ledger-approved TG_SECRET_* references."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

from packages.core.errors import ForbiddenError

_REF = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class SecretResolver:
    def __init__(
        self,
        *,
        approved_refs: Mapping[str, str],
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._approved = dict(approved_refs)
        self._environ = os.environ if environ is None else environ
        if any(not re.fullmatch(r"TG_SECRET_[A-Z0-9_]+", name) for name in self._approved.values()):
            raise ValueError("secret environment names must use the TG_SECRET_* namespace")

    def resolve(self, secret_ref: str, *, principal: str, permission: bool) -> str:
        if principal != "server" or not permission:
            raise ForbiddenError("secret resolution is restricted to authorized server code")
        if (
            not _REF.fullmatch(secret_ref)
            or ".." in secret_ref
            or "/" in secret_ref
            or "\\" in secret_ref
        ):
            raise ForbiddenError("invalid secret_ref")
        env_name = self._approved.get(secret_ref)
        if env_name is None:
            raise ForbiddenError("secret_ref is not approved in the secret ledger")
        value = self._environ.get(env_name)
        if not value:
            raise ForbiddenError("approved secret is missing")
        return str(value)
