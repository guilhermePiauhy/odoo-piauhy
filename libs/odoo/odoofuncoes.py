# libs/odoo/odoofuncoes.py

import base64
import logging
import os
import time
import xmlrpc.client
from typing import Any, Optional

logger = logging.getLogger(__name__)


class Odoo:
    """
    Client wrapper for Odoo XML-RPC API.

    Provides authenticated access to Odoo's common and object endpoints,
    with helpers for record search, read, write, create, and attachment
    retrieval.
    """

    def __init__(
        self,
        url: str,
        db: str,
        username: str,
        password: str,
        max_retries: int = 3,
        retry_delay: float = 2.0,
    ) -> None:
        self.url = url.rstrip("/")
        self.db = db
        self.username = username
        self.password = password
        self.max_retries = max_retries
        self.retry_delay = retry_delay

        self._uid: Optional[int] = None
        self._common: Optional[xmlrpc.client.ServerProxy] = None
        self._models: Optional[xmlrpc.client.ServerProxy] = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_common(self) -> xmlrpc.client.ServerProxy:
        if self._common is None:
            self._common = xmlrpc.client.ServerProxy(
                f"{self.url}/xmlrpc/2/common",
                allow_none=True,
            )
        return self._common

    def _get_models(self) -> xmlrpc.client.ServerProxy:
        if self._models is None:
            self._models = xmlrpc.client.ServerProxy(
                f"{self.url}/xmlrpc/2/object",
                allow_none=True,
            )
        return self._models

    def _execute_with_retry(self, func, *args, **kwargs) -> Any:
        """Execute a callable with exponential-backoff retry logic."""
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return func(*args, **kwargs)
            except (
                xmlrpc.client.Fault,
                xmlrpc.client.ProtocolError,
                ConnectionError,
                OSError,
            ) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    wait = self.retry_delay * attempt
                    logger.warning(
                        "Odoo RPC attempt %d/%d failed (%s). Retrying in %.1fs…",
                        attempt,
                        self.max_retries,
                        exc,
                        wait,
                    )
                    time.sleep(wait)
                else:
                    logger.error(
                        "Odoo RPC failed after %d attempts: %s",
                        self.max_retries,
                        exc,
                    )
        raise last_exc  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self) -> int:
        """Authenticate and cache the user UID. Returns the UID."""
        if self._uid is not None:
            return self._uid

        common = self._get_common()
        uid = self._execute_with_retry(
            common.authenticate,
            self.db,
            self.username,
            self.password,
            {},
        )
        if not uid:
            raise PermissionError(
                f"Odoo authentication failed for user '{self.username}' on db '{self.db}'."
            )
        self._uid = int(uid)
        logger.info("Authenticated with Odoo as UID %d.", self._uid)
        return self._uid

    @property
    def uid(self) -> int:
        return self.authenticate()

    # ------------------------------------------------------------------
    # Core execute_kw wrapper
    # ------------------------------------------------------------------

    def execute(
        self,
        model: str,
        method: str,
        args: list,
        kwargs: Optional[dict] = None,
    ) -> Any:
        """
        Call ``execute_kw`` on the Odoo object endpoint.

        Parameters
        ----------
        model:
            Odoo model name, e.g. ``'project.task'``.
        method:
            ORM method name, e.g. ``'search_read'``.
        args:
            Positional arguments for the method.
        kwargs:
            Keyword arguments for the method (fields, limit, offset, …).
        """
        if kwargs is None:
            kwargs = {}

        models = self._get_models()
        uid = self.uid

        return self._execute_with_retry(
            models.execute_kw,
            self.db,
            uid,
            self.password,
            model,
            method,
            args,
            kwargs,
        )

    # ------------------------------------------------------------------
    # Convenience methods
    # ------------------------------------------------------------------

    def search(
        self,
        model: str,
        domain: list,
        offset: int = 0,
        limit: Optional[int] = None,
        order: Optional[str] = None,
    ) -> list[int]:
        """Return a list of record IDs matching *domain*."""
        kwargs: dict[str, Any] = {"offset": offset}
        if limit is not None:
            kwargs["limit"] = limit
        if order:
            kwargs["order"] = order
        return self.execute(model, "search", [domain], kwargs)

    def read(
        self,
        model: str,
        ids: list[int],
        fields: Optional[list[str]] = None,
    ) -> list[dict]:
        """Read *fields* for records identified by *ids*."""
        kwargs: dict[str, Any] = {}
        if fields:
            kwargs["fields"] = fields
        return self.execute(model, "read", [ids], kwargs)

    def search_read(
        self,
        model: str,
        domain: list,
        fields: Optional[list[str]] = None,
        offset: int = 0,
        limit: Optional[int] = None,
        order: Optional[str] = None,
    ) -> list[dict]:
        """Combined search + read in a single RPC call."""
        kwargs: dict[str, Any] = {"offset": offset}
        if fields:
            kwargs["fields"] = fields
        if limit is not None:
            kwargs["limit"] = limit
        if order:
            kwargs["order"] = order
        return self.execute(model, "search_read", [domain], kwargs)

    def search_count(self, model: str, domain: list) -> int:
        """Return the number of records matching *domain*."""
        return self.execute(model, "search_count", [domain])

    def create(self, model: str, values: dict) -> int:
        """Create a new record and return its ID."""
        return self.execute(model, "create", [values])

    def write(self, model: str, ids: list[int], values: dict) -> bool:
        """Update *ids* with *values*. Returns ``True`` on success."""
        return self.execute(model, "write", [ids, values])

    def unlink(self, model: str, ids: list[int]) -> bool:
        """Delete records identified by *ids*. Returns ``True`` on success."""
        return self.execute(model, "unlink", [ids])

    def fields_get(
        self,
        model: str,
        attributes: Optional[list[str]] = None,
    ) -> dict:
        """Return field metadata for *model*."""
        kwargs: dict[str, Any] = {}
        if attributes:
            kwargs["attributes"] = attributes
        return self.execute(model, "fields_get", [], kwargs)

    # ------------------------------------------------------------------
    # Attachment helpers
    # ------------------------------------------------------------------

    def get_attachments(
        self,
        res_model: str,
        res_ids: list[int],
        fields: Optional[list[str]] = None,
        name_filter: Optional[str] = None,
        mimetype_filter: Optional[str] = None,
    ) -> list[dict]:
        """
        Return ``ir.attachment`` records linked to *res_ids* of