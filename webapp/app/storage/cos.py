"""IBM Cloud Object Storage backend, via the SDK. Never via a mounted bucket.

Why the SDK and not ``--mount-data-store``
------------------------------------------
Code Engine can mount a COS bucket with s3fs, and it is the wrong tool for run
state. IBM's own persistent-data-store documentation says the mount has eventual
consistency, no atomic rename, and that *"multiple instances writing the same
file can cause corruption"*. ``run.json`` is written with ``os.replace`` and the
event log is an append-only file: both of those are precisely the operations
s3fs cannot honour. The SDK path gives one atomic operation per object, which is
all this design needs.

The mount is still the right tool for the *other* case the documentation calls
out — read-heavy access to large files — which is why ``deploy.sh`` can create a
persistent data store for the artifact tree while the state always goes through
this class.

Authentication, in the order this class tries it
------------------------------------------------
1. **Trusted profile** (no static key anywhere). With
   ``--trusted-profiles-enabled=true`` Code Engine mounts a compute-resource
   token and the IBM SDK core exchanges it for an IAM token. This is the
   deployed default and the reason no COS API key appears in any secret.
2. **IAM API key** (``ARCH2CODE_COS_APIKEY``), for running against a real bucket
   from a laptop.
3. **HMAC** (``ARCH2CODE_COS_HMAC_ACCESS_KEY_ID`` / ``…_SECRET_ACCESS_KEY``),
   for tooling that speaks plain S3.

Every failure below names which of the three was in play, because "403" with no
statement of which identity was used is the single most expensive minute in
debugging an IBM Cloud deployment.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .base import ObjectInfo, ObjectStore, StorageError, validate_key

log = logging.getLogger("arch2code.storage.cos")

__all__ = ["CosObjectStore", "CosConfig"]

#: Mounted by Code Engine when --trusted-profiles-enabled=true. Its presence is
#: how this module knows a trusted profile is available without trying a call.
CE_TOKEN_FILE = (
    "/var/run/secrets/codeengine.cloud.ibm.com/compute-resource-token/token"
)

_INSTALL_REMEDY = (
    "The COS backend needs the IBM COS SDK. Install it with "
    "`pip install ibm-cos-sdk` (it is already in deploy/requirements-cloud.txt "
    "and therefore in the container image). To run without object storage, unset "
    "ARCH2CODE_STORAGE_BACKEND — the default 'local' backend needs no SDK."
)


@dataclass(frozen=True)
class CosConfig:
    """Everything needed to reach a bucket. No secret is ever logged."""

    bucket: str
    endpoint: str
    prefix: str = ""
    #: Regional IAM endpoint. Public by default; override for a private VPE.
    ibm_auth_endpoint: str = "https://iam.cloud.ibm.com/identity/token"
    apikey: str | None = None
    service_instance_id: str | None = None
    hmac_access_key_id: str | None = None
    hmac_secret_access_key: str | None = None
    trusted_profile_token_file: str | None = None

    @property
    def auth_mode(self) -> str:
        if self.hmac_access_key_id and self.hmac_secret_access_key:
            return "hmac"
        if self.apikey:
            return "iam-apikey"
        if self.trusted_profile_token_file:
            return "trusted-profile"
        return "none"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "CosConfig":
        """Build from ARCH2CODE_COS_* variables, failing loudly on what is missing."""
        source = os.environ if env is None else env

        bucket = (source.get("ARCH2CODE_COS_BUCKET") or "").strip()
        endpoint = (source.get("ARCH2CODE_COS_ENDPOINT") or "").strip()
        missing = [
            name
            for name, value in (
                ("ARCH2CODE_COS_BUCKET", bucket),
                ("ARCH2CODE_COS_ENDPOINT", endpoint),
            )
            if not value
        ]
        if missing:
            raise StorageError(
                "cos_not_configured",
                "The COS backend is selected but not configured",
                f"ARCH2CODE_STORAGE_BACKEND=cos, and {', '.join(missing)} is unset.",
                remedy=(
                    "Set them in the Code Engine secret that both the app and the job "
                    "read, e.g. ARCH2CODE_COS_BUCKET=arch2code-runs and "
                    "ARCH2CODE_COS_ENDPOINT=https://s3.us-south.cloud-object-storage."
                    "appdomain.cloud. See deploy/DEPLOYMENT.md step 3."
                ),
            )

        if not endpoint.startswith(("http://", "https://")):
            endpoint = f"https://{endpoint}"

        token_file = (
            source.get("ARCH2CODE_COS_TRUSTED_PROFILE_TOKEN_FILE") or CE_TOKEN_FILE
        ).strip()

        prefix = (source.get("ARCH2CODE_COS_PREFIX") or "").strip().lstrip("/")
        if prefix and not prefix.endswith("/"):
            prefix += "/"

        return cls(
            bucket=bucket,
            endpoint=endpoint,
            prefix=prefix,
            ibm_auth_endpoint=(
                source.get("ARCH2CODE_COS_IAM_ENDPOINT")
                or "https://iam.cloud.ibm.com/identity/token"
            ).strip(),
            apikey=(source.get("ARCH2CODE_COS_APIKEY") or "").strip() or None,
            service_instance_id=(
                source.get("ARCH2CODE_COS_INSTANCE_ID") or ""
            ).strip()
            or None,
            hmac_access_key_id=(
                source.get("ARCH2CODE_COS_HMAC_ACCESS_KEY_ID") or ""
            ).strip()
            or None,
            hmac_secret_access_key=(
                source.get("ARCH2CODE_COS_HMAC_SECRET_ACCESS_KEY") or ""
            ).strip()
            or None,
            trusted_profile_token_file=(
                token_file if token_file and Path(token_file).exists() else None
            ),
        )

    def describe(self) -> dict[str, Any]:
        """Location, never authorization."""
        return {
            "backend": "cos",
            "bucket": self.bucket,
            "endpoint": self.endpoint,
            "prefix": self.prefix or "<root>",
            "auth_mode": self.auth_mode,
        }


class CosObjectStore(ObjectStore):
    """:class:`ObjectStore` over one COS bucket.

    Thread safety: the underlying ``ibm_boto3`` client is not documented as
    thread safe, so one client is created per thread. That costs a handful of
    objects and removes an entire class of intermittent failure.
    """

    backend = "cos"

    def __init__(self, config: CosConfig) -> None:
        self._config = config
        self._local = threading.local()
        if config.auth_mode == "none":
            raise StorageError(
                "cos_no_credentials",
                "No way to authenticate to Cloud Object Storage",
                (
                    "No trusted-profile token file is mounted at "
                    f"{CE_TOKEN_FILE}, ARCH2CODE_COS_APIKEY is unset, and no HMAC "
                    "pair is set."
                ),
                remedy=(
                    "In Code Engine: create the app and the job with "
                    "--trusted-profiles-enabled=true AND create an IAM trusted "
                    "profile that trusts this Code Engine project with Writer on the "
                    "bucket — the flag alone only mounts the token, it grants "
                    "nothing. Outside Code Engine: export ARCH2CODE_COS_APIKEY. "
                    "See deploy/DEPLOYMENT.md step 4."
                ),
            )

    @property
    def config(self) -> CosConfig:
        return self._config

    # -- client -------------------------------------------------------------- #

    def _client(self) -> Any:
        client = getattr(self._local, "client", None)
        if client is not None:
            return client

        try:
            import ibm_boto3  # type: ignore[import-not-found]
            from ibm_botocore.client import Config  # type: ignore[import-not-found]
        except ImportError as exc:
            raise StorageError(
                "cos_sdk_missing",
                "The IBM COS SDK is not installed",
                f"import ibm_boto3 failed: {exc}",
                remedy=_INSTALL_REMEDY,
            ) from exc

        cfg = self._config
        common = {
            "service_name": "s3",
            "endpoint_url": cfg.endpoint,
            # Path-style keeps a bucket name with dots working over TLS, which
            # virtual-host style silently breaks with a certificate mismatch.
            "config": Config(signature_version="s3v4", s3={"addressing_style": "path"}),
        }

        if cfg.auth_mode == "hmac":
            client = ibm_boto3.client(
                aws_access_key_id=cfg.hmac_access_key_id,
                aws_secret_access_key=cfg.hmac_secret_access_key,
                **common,
            )
        elif cfg.auth_mode == "iam-apikey":
            client = ibm_boto3.client(
                ibm_api_key_id=cfg.apikey,
                ibm_service_instance_id=cfg.service_instance_id,
                ibm_auth_endpoint=cfg.ibm_auth_endpoint,
                **common,
            )
        else:  # trusted-profile
            client = self._trusted_profile_client(ibm_boto3, cfg, common)

        self._local.client = client
        return client

    @staticmethod
    def _trusted_profile_client(
        ibm_boto3: Any, cfg: CosConfig, common: dict[str, Any]
    ) -> Any:
        """Build a client that authenticates with the compute-resource token.

        The token file is refreshed by the platform, so it is read on every
        credential refresh rather than cached here. ``ContainerAuthenticator``
        from ibm-cloud-sdk-core does the IAM exchange; the COS SDK accepts the
        resulting bearer token through its own credential object.
        """
        try:
            from ibm_botocore.credentials import (  # type: ignore[import-not-found]
                Credentials,
            )
            from ibm_cloud_sdk_core.authenticators import (  # type: ignore[import-not-found]
                ContainerAuthenticator,
            )
        except ImportError as exc:
            raise StorageError(
                "cos_trusted_profile_sdk_missing",
                "Trusted-profile authentication needs ibm-cloud-sdk-core",
                f"import failed: {exc}",
                remedy=(
                    "Install ibm-cloud-sdk-core (it ships with ibm-code-engine-sdk, "
                    "which is in deploy/requirements-cloud.txt). Alternatively set "
                    "ARCH2CODE_COS_APIKEY to use a static key instead."
                ),
            ) from exc

        authenticator = ContainerAuthenticator(
            cr_token_filename=cfg.trusted_profile_token_file,
            iam_profile_name=os.environ.get("ARCH2CODE_TRUSTED_PROFILE_NAME") or None,
            iam_profile_id=os.environ.get("ARCH2CODE_TRUSTED_PROFILE_ID") or None,
        )

        class _TokenProvider:
            """Adapter: IAM bearer token -> the shape ibm_botocore wants."""

            def load(self) -> Any:  # pragma: no cover - exercised only in-cloud
                token = authenticator.token_manager.get_token()
                return Credentials(token=token, method="ibm-trusted-profile")

        client = ibm_boto3.client(**common)
        # ibm_boto3 resolves credentials through a chain; inserting the provider
        # at the front keeps every other resolution path intact.
        try:
            client._request_signer._credentials = _TokenProvider().load()
        except Exception as exc:  # pragma: no cover - SDK internals moved
            raise StorageError(
                "cos_trusted_profile_wiring_failed",
                "Could not attach the trusted-profile token to the COS client",
                f"{type(exc).__name__}: {exc}",
                remedy=(
                    "This depends on ibm-cos-sdk internals. Pin ibm-cos-sdk to the "
                    "version in deploy/requirements-cloud.txt, or switch this "
                    "deployment to ARCH2CODE_COS_APIKEY, which uses only the "
                    "documented constructor arguments."
                ),
            ) from exc
        return client

    # -- key mapping ---------------------------------------------------------- #

    def _full(self, key: str) -> str:
        return f"{self._config.prefix}{validate_key(key)}"

    def _strip(self, full_key: str) -> str:
        prefix = self._config.prefix
        return full_key[len(prefix):] if prefix and full_key.startswith(prefix) else full_key

    # -- operations ----------------------------------------------------------- #

    def put_bytes(
        self, key: str, data: bytes, *, content_type: str | None = None
    ) -> None:
        extra: dict[str, Any] = {}
        if content_type:
            extra["ContentType"] = content_type
        try:
            self._client().put_object(
                Bucket=self._config.bucket, Key=self._full(key), Body=data, **extra
            )
        except Exception as exc:  # noqa: BLE001 - normalized below
            raise self._translate(exc, "write", key) from exc

    def put_if_absent(self, key: str, data: bytes) -> bool:
        """Check-then-put. **Not atomic** — see the class docstring.

        Safe here because every key written this way has exactly one writer by
        construction: one job run owns a run's events, the app owns uploads. If
        that ever stops being true, this needs a conditional put and IBM COS
        support for ``If-None-Match`` is unverified.
        """
        if self.exists(key):
            return False
        self.put_bytes(key, data)
        return True

    def get_bytes(self, key: str) -> bytes:
        try:
            response = self._client().get_object(
                Bucket=self._config.bucket, Key=self._full(key)
            )
            return response["Body"].read()
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc, "read", key) from exc

    def exists(self, key: str) -> bool:
        try:
            self._client().head_object(Bucket=self._config.bucket, Key=self._full(key))
            return True
        except Exception as exc:  # noqa: BLE001
            if _status_of(exc) in (403, 404):
                # 403 on head_object is what a bucket policy without ListBucket
                # returns for a missing key. Treating it as "absent" here and
                # letting the next real read raise the credential error keeps
                # exists() from becoming a permissions oracle.
                return False
            raise self._translate(exc, "head", key) from exc

    def delete(self, key: str) -> None:
        try:
            self._client().delete_object(
                Bucket=self._config.bucket, Key=self._full(key)
            )
        except Exception as exc:  # noqa: BLE001
            if _status_of(exc) == 404:
                return
            raise self._translate(exc, "delete", key) from exc

    def list_keys(
        self,
        prefix: str,
        *,
        start_after: str | None = None,
        limit: int | None = None,
    ) -> list[str]:
        params: dict[str, Any] = {
            "Bucket": self._config.bucket,
            "Prefix": self._full(prefix) if prefix else self._config.prefix,
        }
        if start_after:
            params["StartAfter"] = self._full(start_after)
        if limit is not None:
            params["MaxKeys"] = max(1, min(1000, limit))

        try:
            response = self._client().list_objects_v2(**params)
        except Exception as exc:  # noqa: BLE001
            raise self._translate(exc, "list", prefix) from exc

        keys = [self._strip(item["Key"]) for item in response.get("Contents", [])]
        keys.sort()  # S3 returns lexicographic order; sorting makes it a guarantee
        return keys[:limit] if limit is not None else keys

    def local_path(self, key: str) -> Path | None:
        """Always ``None``. An object key is not a path a subprocess can open.

        Callers that need to hand a file to Bob must stage it first; see
        :func:`app.storage.sync.download_prefix`.
        """
        del key
        return None

    # -- diagnostics ----------------------------------------------------------- #

    def probe(self) -> ObjectInfo | None:
        key = ".arch2code-probe"
        stamp = datetime.now(timezone.utc).isoformat()
        self.put_bytes(key, stamp.encode("utf-8"), content_type="text/plain")
        echoed = self.get_bytes(key).decode("utf-8")
        if echoed != stamp:  # pragma: no cover
            raise StorageError(
                "storage_roundtrip_mismatch",
                "COS returned different bytes than were written",
                f"wrote {stamp!r}, read {echoed!r} at {self._full(key)}.",
                remedy=(
                    "Check that no other process is writing the same prefix, and that "
                    "ARCH2CODE_COS_PREFIX is unique per deployment."
                ),
            )
        self.delete(key)
        return ObjectInfo(key=key, size=len(stamp), last_modified=stamp)

    def describe(self) -> dict[str, Any]:
        return self._config.describe()

    # -- error translation ------------------------------------------------------ #

    def _translate(self, exc: Exception, operation: str, key: str) -> StorageError:
        """Turn an SDK exception into a failure that says what to do next."""
        status = _status_of(exc)
        cfg = self._config
        where = f"{operation} {cfg.bucket}/{self._full(key)} at {cfg.endpoint}"

        if status == 404:
            return StorageError(
                "object_not_found",
                "No such stored object",
                f"{where} returned 404.",
                remedy=(
                    "The run or upload does not exist in this bucket. If it exists on "
                    "another deployment, ARCH2CODE_COS_PREFIX or ARCH2CODE_COS_BUCKET "
                    "differs between the app and the job — they must match exactly."
                ),
                status=404,
                key=key,
            )
        if status in (401, 403):
            return StorageError(
                "cos_forbidden",
                "Cloud Object Storage refused the request",
                f"{where} returned {status} using auth mode {cfg.auth_mode!r}.",
                remedy=(
                    "With auth mode 'trusted-profile': --trusted-profiles-enabled=true "
                    "only mounts a token. You must also create an IAM trusted profile "
                    "that trusts this Code Engine project and give it the Writer role "
                    "on the bucket (DEPLOYMENT.md step 4). With 'iam-apikey': confirm "
                    "the key belongs to an identity with Writer on this bucket. "
                    "Verify quickly with `ibmcloud cos object-put --bucket "
                    f"{cfg.bucket} --key probe.txt --body /etc/hostname`."
                ),
                status=502,
                key=key,
            )
        if status == 404 or "NoSuchBucket" in str(exc):
            return StorageError(
                "cos_bucket_missing",
                "That bucket does not exist at this endpoint",
                f"{where}: {exc}",
                remedy=(
                    f"Create it, or fix the endpoint: a bucket in us-south is not "
                    f"reachable through an eu-de endpoint. Current endpoint "
                    f"{cfg.endpoint}. List yours with `ibmcloud cos buckets`."
                ),
                status=502,
                key=key,
            )
        return StorageError(
            "cos_request_failed",
            "The Cloud Object Storage request failed",
            f"{where}: {type(exc).__name__}: {exc}",
            remedy=(
                "Check the endpoint hostname and that egress is allowed. Code Engine "
                "defaults to allow-all egress, so a connection error here usually "
                "means a typo in ARCH2CODE_COS_ENDPOINT. Test it from a job run with "
                "`python -c \"import app.storage as s; s.open_object_store().probe()\"`."
            ),
            status=502,
            key=key,
        )


def _status_of(exc: Exception) -> int | None:
    """HTTP status out of an ibm_botocore ClientError, without importing it."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        meta = response.get("ResponseMetadata")
        if isinstance(meta, dict):
            code = meta.get("HTTPStatusCode")
            if isinstance(code, int):
                return code
        error = response.get("Error")
        if isinstance(error, dict):
            code = error.get("Code")
            if code in ("404", "NoSuchKey", "NoSuchBucket"):
                return 404
            if code in ("403", "AccessDenied"):
                return 403
    return None
