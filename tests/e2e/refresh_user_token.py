"""Mint a fresh Feishu user token for CI and persist the rotated refresh token.

The helper never prints token values. It exchanges the repository's rotating
refresh-token secret immediately before a Feishu-touching job, updates that
secret through `gh secret set` over stdin, and only then appends the short-lived
user access token to GitHub Actions' GITHUB_ENV file.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

APP_TOKEN_PATH = "/open-apis/auth/v3/app_access_token/internal"
REFRESH_PATH = "/open-apis/authen/v1/refresh_access_token"
REFRESH_SECRET_NAME = "E2E_SIMULATOR_USER_REFRESH_TOKEN"
ACCESS_TOKEN_ENV_NAME = "E2E_SIMULATOR_USER_ACCESS_TOKEN"


class TokenRefreshError(RuntimeError):
    """The CI token exchange or rotated-secret persistence failed."""


@dataclass(frozen=True)
class RefreshedUserToken:
    access_token: str
    refresh_token: str


def _required(env, name: str) -> str:
    value = env.get(name, "")
    if not value:
        raise TokenRefreshError(f"missing token refresh configuration: {name}")
    return value


def _secret_value(value, field_name: str) -> str:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise TokenRefreshError(f"Feishu token response has invalid {field_name}")
    return value


def mask_github_value(value: str) -> None:
    """Register a generated value with the Actions runner before later steps."""
    # GitHub workflow commands require percent escaping. Token newlines were
    # already rejected by _secret_value. The runner consumes this command and
    # redacts the value from the job log and subsequent command output.
    escaped = value.replace("%", "%25")
    print(f"::add-mask::{escaped}", flush=True)


def _request_json(url: str, body: dict, *, authorization: str = "", urlopen=None) -> dict:
    opener = urlopen or urllib.request.urlopen
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if authorization:
        headers["Authorization"] = "Bearer " + authorization
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with opener(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8", errors="replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        code = payload.get("code", e.code)
        message = payload.get("msg") or e.reason
        raise TokenRefreshError(
            f"Feishu token request failed (HTTP {e.code}, code {code}): {message}"
        ) from e
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise TokenRefreshError(f"Feishu token request failed: {e}") from e
    if not isinstance(payload, dict) or payload.get("code") != 0:
        code = payload.get("code", "unknown") if isinstance(payload, dict) else "unknown"
        message = payload.get("msg", "invalid response") if isinstance(payload, dict) else "invalid response"
        raise TokenRefreshError(
            f"Feishu token request failed (code {code}): {message}"
        )
    return payload


def refresh_user_token(
    app_id: str,
    app_secret: str,
    refresh_token: str,
    *,
    base_url: str = "https://open.feishu.cn",
    urlopen=None,
) -> RefreshedUserToken:
    """Exchange one rotating refresh token for a fresh token pair."""
    app_payload = _request_json(
        base_url.rstrip("/") + APP_TOKEN_PATH,
        {"app_id": app_id, "app_secret": app_secret},
        urlopen=urlopen,
    )
    app_access_token = _secret_value(
        app_payload.get("app_access_token"), "app_access_token"
    )
    refresh_payload = _request_json(
        base_url.rstrip("/") + REFRESH_PATH,
        {"grant_type": "refresh_token", "refresh_token": refresh_token},
        authorization=app_access_token,
        urlopen=urlopen,
    )
    data = refresh_payload.get("data") or {}
    return RefreshedUserToken(
        access_token=_secret_value(
            data.get("access_token") or data.get("user_access_token"),
            "user access token",
        ),
        refresh_token=_secret_value(data.get("refresh_token"), "refresh_token"),
    )


def rotate_github_secret(
    refresh_token: str,
    repository: str,
    github_token: str,
    *,
    attempts: int = 3,
    run=subprocess.run,
    sleep=time.sleep,
) -> None:
    """Persist the rotated token over stdin, retrying transient write failures."""
    inherited = os.environ
    gh_env = {
        name: inherited[name]
        for name in (
            "PATH",
            "HOME",
            "XDG_CONFIG_HOME",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
        )
        if name in inherited
    }
    gh_env["GH_TOKEN"] = github_token
    if attempts <= 0:
        raise ValueError("attempts must be positive")
    last_detail = "unknown failure"
    for attempt in range(1, attempts + 1):
        try:
            completed = run(
                [
                    "gh",
                    "secret",
                    "set",
                    REFRESH_SECRET_NAME,
                    "--repo",
                    repository,
                    "--app",
                    "actions",
                ],
                input=refresh_token,
                text=True,
                capture_output=True,
                env=gh_env,
                timeout=60,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as e:
            last_detail = str(e)
        else:
            if completed.returncode == 0:
                return
            last_detail = (
                (completed.stderr or "").strip()
                or f"exit {completed.returncode}"
            )
        if attempt < attempts:
            sleep(min(2 ** (attempt - 1), 4))
    raise TokenRefreshError(
        "GitHub secret rotation failed after "
        f"{attempts} attempts: {last_detail}; the consumed Feishu refresh "
        "token must be replaced through a new interactive OAuth grant"
    )


def append_github_env(path: str, access_token: str) -> None:
    """Expose the fresh access token only to later steps in the current job."""
    token = _secret_value(access_token, "user access token")
    target = Path(path)
    with open(target, "a", encoding="utf-8") as stream:
        stream.write(f"{ACCESS_TOKEN_ENV_NAME}={token}\n")


def run_ci_refresh(env=None, *, urlopen=None, run=subprocess.run) -> None:
    env = env if env is not None else os.environ
    refreshed = refresh_user_token(
        _required(env, "E2E_BRIDGE_FEISHU_APP_ID"),
        _required(env, "E2E_BRIDGE_FEISHU_APP_SECRET"),
        _required(env, REFRESH_SECRET_NAME),
        base_url=env.get("E2E_BRIDGE_FEISHU_BASE_URL", "https://open.feishu.cn"),
        urlopen=urlopen,
    )
    # Generated values are not repository secrets yet. Register them with the
    # Actions runner before any subsequent command could expose them.
    if env.get("GITHUB_ACTIONS", "").lower() == "true":
        mask_github_value(refreshed.access_token)
        mask_github_value(refreshed.refresh_token)
    # Feishu invalidates the old refresh token when it returns the new one.
    # Persist the rotated value before making the short-lived access token
    # available, so a failed write cannot masquerade as a healthy CI run.
    rotate_github_secret(
        refreshed.refresh_token,
        _required(env, "GITHUB_REPOSITORY"),
        _required(env, "E2E_SIMULATOR_GITHUB_SECRETS_PAT"),
        run=run,
    )
    append_github_env(
        _required(env, "GITHUB_ENV"),
        refreshed.access_token,
    )


def main() -> int:
    try:
        run_ci_refresh()
    except TokenRefreshError as e:
        print(f"token refresh failed: {e}", file=sys.stderr)
        return 1
    print("Feishu test-user token refreshed and rotated secret persisted")
    return 0


if __name__ == "__main__":
    sys.exit(main())
